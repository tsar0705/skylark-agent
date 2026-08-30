"""
Groq-powered conversational BI agent.

Uses Groq's OpenAI-compatible Chat Completions API together with the
existing project analysis tools.

Design goals:
- Low token usage for Groq free/on-demand limits.
- Strict grounding in live monday.com data.
- Exact dataset terminology.
- Pipeline always means Deal Status = Open unless explicitly stated otherwise.
- No invented dates, currencies, statuses, metrics, or business terminology.
- Valid JSON tool arguments only.
- No duplicate tool calls.
- Failed optional tools do not automatically invalidate successful results.
- Read-only monday.com access.
"""

import json

from groq import Groq

from .config import settings
from .tools import (
    TOOL_SCHEMAS,
    ToolRunner,
    DataStore,
    ToolExecutionError,
)


# ---------------------------------------------------------------------------
# COMPACT SYSTEM PROMPT
# ---------------------------------------------------------------------------
# Keep this short deliberately. It is sent to Groq on every chat request.
# The important data-safety rules are retained without wasting thousands
# of tokens on repeated examples.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are Skylark Drones' internal BI agent.

Answer ONLY from successful results returned by the available monday.com
analysis tools. Never invent numbers or facts.

DATASETS
- work_orders: execution, dates, billing, collections, quantities, sector.
- deals: Deal Status, Deal Stage, Sector/service, Masked Deal value,
  Closure Probability and dates.

STRICT RULES

1. GROUNDING
Every number, count, percentage, date, status, sector, stage and metric in
your answer must come from:
- the user's message, or
- a successful tool result.

Never guess or fabricate data.

2. EXACT TERMINOLOGY
Use the actual dataset column names and values.

Important deals fields:
- Deal Status
- Deal Stage
- Sector/service
- Masked Deal value
- Closure Probability
- Close Date (A)
- Tentative Close Date
- Created Date

Important work-order fields:
- Execution Status
- Billing Status
- Invoice Status
- WO Status (billed)
- Sector
- Probable Start Date
- Probable End Date
- Amount Receivable (Masked)
- Collected Amount in Rupees (Incl. of GST.) (Masked)

Do not silently rename fields.

Examples:
- Deal Status is NOT "pipeline status".
- Deal Stage is NOT "sales status".
- Open is NOT automatically "Active".
- Not Started is NOT automatically "Stuck".
- Masked Deal value is NOT automatically "revenue".
- Won is NOT automatically "revenue".

3. PIPELINE DEFINITION
When the user says "pipeline", "current pipeline", "pipeline value",
"how's our pipeline", or similar, use:

Deal Status = "Open"

For a sector pipeline question, also filter:

Sector/service = the requested sector.

Do NOT include Won or Dead deals in pipeline calculations unless the user
explicitly asks for them.

4. DEAL STATUS VS DEAL STAGE
They are different fields.

Use only values established by the data/tools.
Do not change "Dead" to "Lost", or "Open" to "Active".

5. CURRENCY
Never invent a currency.

Do not use $, USD, EUR, GBP, dollars, etc. unless explicitly established
by the data/result.

For masked monetary fields, prefer "recorded value" or the application's
INR convention when appropriate.

Never convert currencies.

6. DATES
Never invent dates or reporting periods.

Do not add:
- FY2025
- FY2026
- 2024-2025
- this quarter
- last quarter
- as of today
- current month

unless the user explicitly asks for that period or a successful tool result
establishes it.

"Right now" means the records currently available in the dataset. It does
not authorize inventing today's date.

7. TOOL USAGE
Use:
- get_schema when column/dataset names need confirmation.
- run_analysis for calculations.
- get_data_quality_notes when missing/invalid data materially affects the
  requested answer or the user asks about quality.

Prefer 1-3 tool calls.

Use the smallest valid analysis that answers the question.

8. COMMON QUERIES

"How many work orders are there?"
=> run_analysis:
dataset=work_orders, operation=count

"What's our total amount receivable?"
=> run_analysis:
dataset=work_orders,
column="Amount Receivable (Masked)",
operation=sum

"Compare won deal value by sector"
=> run_analysis:
dataset=deals,
column="Masked Deal value",
filters=[Deal Status equals Won],
group_by="Sector/service",
operation=group_sum

"How many open Mining deals?"
=> run_analysis:
dataset=deals,
filters=[
  Sector/service equals Mining,
  Deal Status equals Open
],
operation=count

"Mining pipeline value"
=> run_analysis:
dataset=deals,
column="Masked Deal value",
filters=[
  Sector/service equals Mining,
  Deal Status equals Open
],
operation=sum

9. TOOL ARGUMENTS
Tool arguments MUST be valid JSON objects matching the supplied schema.

Correct:
{"dataset":"work_orders","operation":"count"}

Never generate Python as tool arguments.

Never generate:
result = ...
work_orders[...]
pd.Timestamp(...)
Python expressions
or arbitrary code.

10. FAILED TOOLS
If a tool fails:
- do not repeat the exact same call;
- use successful results already available;
- simplify the analysis;
- use another supported operation if necessary.

If successful results are sufficient, answer anyway.

Do not say "I couldn't finish" merely because an optional call failed.

11. DUPLICATES
Never make the exact same tool call twice.

12. DATA QUALITY
Do not silently treat missing values as zero.

If an aggregate excludes missing/non-numeric values and the tool provides
coverage information, mention it briefly.

If data quality is not material to the requested answer, do not waste a
tool call on it.

13. INTERPRETATION
You may give short conclusions that logically follow from the data.

Do not claim causation.

Bad:
"Mining is weak because the sales team is slow."

Good:
"Mining has lower recorded open pipeline value than Railways."

14. ANSWER STYLE
Lead with the answer.

Simple question:
- one headline
- 1-3 supporting bullets

Comparison:
- concise markdown table
- 2-3 takeaways

Pipeline:
- clearly use Deal Status = Open
- give count and/or recorded value
- mention important data-quality caveats
- do not unnecessarily discuss Won/Dead

Keep answers concise and executive-friendly.

15. NO UNSUPPORTED BUSINESS TERMS
Do not introduce terms such as:
revenue, ARR, bookings, forecast, fiscal year, conversion rate,
win rate, active customer, closed won, lost pipeline, sales velocity

unless the requested calculation is explicitly supported by the data.

16. READ ONLY
The monday.com data is read-only.

Never claim to have changed, created, deleted, updated or written anything
to monday.com.
"""


class Agent:
    """
    Conversational BI agent using Groq function calling.
    """

    def __init__(self):
        api_key = settings.GROQ_API_KEY

        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set in backend/.env"
            )

        self.client = Groq(api_key=api_key)
        self.model = settings.GROQ_MODEL

        self.store = DataStore()
        self.runner = ToolRunner(self.store)

        self.tools = self._tool_schemas_for_groq()

    # ------------------------------------------------------------------
    # TOOL SCHEMA CONVERSION
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_schemas_for_groq() -> list[dict]:
        """
        Convert the project's existing tool schemas into the
        OpenAI/Groq function-tool format.
        """

        tools = []

        for schema in TOOL_SCHEMAS:
            if not isinstance(schema, dict):
                continue

            name = schema.get("name")

            if not name:
                continue

            description = schema.get(
                "description",
                "",
            )

            parameters = schema.get(
                "input_schema",
                schema.get("parameters", {}),
            )

            if not isinstance(parameters, dict):
                parameters = {}

            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )

        return tools

    # ------------------------------------------------------------------
    # DUPLICATE CALL DETECTION
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_call_key(
        tool_name: str,
        arguments: dict,
    ) -> tuple:
        """
        Create a deterministic key for a tool call.

        This prevents the model from repeatedly executing the exact same
        failing or successful operation.
        """

        try:
            serialized = json.dumps(
                arguments,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            serialized = str(arguments)

        return tool_name, serialized

    # ------------------------------------------------------------------
    # SAFE TOOL RESULT SERIALIZATION
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_tool_output(output) -> str:
        """
        Convert tool output into compact JSON/text for Groq.
        """

        if isinstance(output, (dict, list)):
            try:
                return json.dumps(
                    output,
                    default=str,
                    separators=(",", ":"),
                )
            except Exception:
                return str(output)

        return str(output)

    # ------------------------------------------------------------------
    # ERROR MESSAGE
    # ------------------------------------------------------------------

    @staticmethod
    def _friendly_model_error(error: Exception) -> str:
        """
        Avoid exposing a huge Groq exception object to the frontend.
        """

        message = str(error)

        lower = message.lower()

        if (
            "429" in message
            or "rate_limit" in lower
            or "rate limit" in lower
            or "tokens per day" in lower
            or "tokens per minute" in lower
        ):
            return (
                "The AI service has temporarily reached its usage limit. "
                "Please try again shortly."
            )

        if (
            "tool_use_failed" in lower
            or "failed to parse tool call" in lower
            or "json" in lower and "tool" in lower
        ):
            return (
                "The AI model returned an invalid tool request. "
                "Please try the question again."
            )

        if "model_not_found" in lower:
            return (
                f"The configured Groq model '{settings.GROQ_MODEL}' "
                "is unavailable. Check GROQ_MODEL in backend/.env."
            )

        return (
            "I couldn't complete the analysis because the AI model "
            "returned an error."
        )

    # ------------------------------------------------------------------
    # MAIN RESPONSE LOOP
    # ------------------------------------------------------------------

    def respond(
        self,
        conversation: list[dict],
        max_tool_iterations: int = 4,
    ) -> dict:
        """
        Run the conversational BI agent.

        Parameters
        ----------
        conversation:
            List of:
                {"role": "user", "content": "..."}
                {"role": "assistant", "content": "..."}

        max_tool_iterations:
            Maximum number of model/tool rounds.

        Returns
        -------
        {
            "reply": "...",
            "tool_trace": [...]
        }
        """

        # --------------------------------------------------------------
        # Build messages
        # --------------------------------------------------------------

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]

        for item in conversation:

            if not isinstance(item, dict):
                continue

            role = item.get("role")
            content = item.get("content")

            if role not in {"user", "assistant"}:
                continue

            if content is None:
                continue

            messages.append(
                {
                    "role": role,
                    "content": str(content),
                }
            )

        tool_trace = []

        # Exact calls already attempted during this request.
        attempted_calls = set()

        # Successful outputs retained locally so we can tell the model
        # that useful information already exists.
        successful_results = []

        # --------------------------------------------------------------
        # Tool / model loop
        # --------------------------------------------------------------

        for _ in range(max_tool_iterations):

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=self.tools,
                    tool_choice="auto",
                    temperature=0,
                    max_tokens=900,
                )

            except Exception as error:

                # If the model failed after we already have useful tool
                # results, don't pretend the data disappeared.
                if successful_results:
                    return {
                        "reply": (
                            "I obtained the required data, but the AI "
                            "service could not finish formatting the "
                            "answer. Please try the same question again."
                        ),
                        "tool_trace": tool_trace,
                    }

                return {
                    "reply": self._friendly_model_error(error),
                    "tool_trace": tool_trace,
                }

            # ----------------------------------------------------------
            # No choices
            # ----------------------------------------------------------

            if not response.choices:

                return {
                    "reply": (
                        "I couldn't generate a response from the "
                        "AI model."
                    ),
                    "tool_trace": tool_trace,
                }

            message = response.choices[0].message

            # ----------------------------------------------------------
            # FINAL TEXT ANSWER
            # ----------------------------------------------------------

            if not message.tool_calls:

                text = message.content or ""

                if text.strip():

                    return {
                        "reply": text.strip(),
                        "tool_trace": tool_trace,
                    }

                # The model returned no text after successful analysis.
                # Give it one short chance to format the answer if budget
                # remains.
                if successful_results:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                        }
                    )

                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Using the successful tool results above, "
                                "give the concise final answer. Do not call "
                                "another tool unless absolutely necessary."
                            ),
                        }
                    )

                    continue

                return {
                    "reply": (
                        "I couldn't generate a complete answer from "
                        "the available analysis results."
                    ),
                    "tool_trace": tool_trace,
                }

            # ----------------------------------------------------------
            # Preserve assistant tool calls exactly as required by Groq
            # ----------------------------------------------------------

            assistant_tool_calls = []

            for tool_call in message.tool_calls:

                assistant_tool_calls.append(
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": (
                                tool_call.function.arguments or "{}"
                            ),
                        },
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": assistant_tool_calls,
                }
            )

            # ----------------------------------------------------------
            # Execute each requested tool
            # ----------------------------------------------------------

            for tool_call in message.tool_calls:

                tool_name = tool_call.function.name

                raw_arguments = (
                    tool_call.function.arguments or "{}"
                )

                # ------------------------------------------------------
                # Parse JSON
                # ------------------------------------------------------

                try:

                    arguments = json.loads(
                        raw_arguments
                    )

                    if not isinstance(arguments, dict):
                        raise ValueError(
                            "Tool arguments must be a JSON object."
                        )

                except Exception as error:

                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "input": raw_arguments,
                            "error": True,
                        }
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": (
                                "Invalid JSON arguments. "
                                "Return a valid JSON object matching "
                                "the tool schema. "
                                f"Parser error: {error}"
                            ),
                        }
                    )

                    continue

                # ------------------------------------------------------
                # Prevent duplicate calls
                # ------------------------------------------------------

                call_key = self._canonical_call_key(
                    tool_name,
                    arguments,
                )

                if call_key in attempted_calls:

                    tool_trace.append(
                        {
                            "tool": tool_name,
                            "input": arguments,
                            "error": True,
                            "duplicate": True,
                        }
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": (
                                "Duplicate call. This exact operation "
                                "was already attempted. Do not repeat "
                                "it. Use the previous result or choose "
                                "a different supported operation."
                            ),
                        }
                    )

                    continue

                attempted_calls.add(call_key)

                # ------------------------------------------------------
                # Execute
                # ------------------------------------------------------

                try:

                    output = self.runner.execute(
                        tool_name,
                        arguments,
                    )

                    is_error = False

                except ToolExecutionError as error:

                    output = str(error)
                    is_error = True

                except Exception as error:

                    output = (
                        f"Unexpected tool error: {error}"
                    )
                    is_error = True

                # ------------------------------------------------------
                # Trace
                # ------------------------------------------------------

                trace_item = {
                    "tool": tool_name,
                    "input": arguments,
                    "error": is_error,
                }

                tool_trace.append(trace_item)

                # ------------------------------------------------------
                # Serialize
                # ------------------------------------------------------

                tool_content = self._serialize_tool_output(
                    output
                )

                # ------------------------------------------------------
                # Remember successful result
                # ------------------------------------------------------

                if not is_error:

                    successful_results.append(
                        {
                            "tool": tool_name,
                            "result": tool_content,
                        }
                    )

                # ------------------------------------------------------
                # Send result back to Groq
                # ------------------------------------------------------

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_content,
                    }
                )

        # ------------------------------------------------------------------
        # Tool budget exhausted
        # ------------------------------------------------------------------

        if successful_results:

            return {
                "reply": (
                    "I obtained some analysis results, but the AI model "
                    "did not finish the final response within the "
                    "tool-call limit. Please try the question again."
                ),
                "tool_trace": tool_trace,
            }

        return {
            "reply": (
                "I couldn't complete the analysis within the "
                "tool-call limit. I won't guess at the result."
            ),
            "tool_trace": tool_trace,
        }