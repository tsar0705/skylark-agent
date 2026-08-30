"""
Leadership update generator using Groq.

The expensive/messy part of the leadership report is calculated
deterministically by the leadership_summary tool.

Groq is responsible for turning those verified numbers into a concise
leadership-facing markdown report.
"""

import json
import os

from groq import Groq

from .tools import (
    TOOL_SCHEMAS,
    ToolRunner,
    DataStore,
    ToolExecutionError,
)


LEADERSHIP_SYSTEM_PROMPT = """
You are the leadership-report writer for Skylark Drones.

Your job is to turn the VERIFIED output of the `leadership_summary` tool
into a concise executive update.

IMPORTANT DATA RULES:

1. Use ONLY numbers, statuses, sectors, stages, field names and definitions
   that appear in the tool output.

2. NEVER invent a date, reporting period, currency, terminology, status,
   sector, stage, client, deal name, percentage or business definition.

3. Do NOT call something "pipeline" unless the tool's definition supports it.

4. The open pipeline is specifically defined by the tool output.
   Follow that definition exactly.

5. The won value is based on the period stated by the tool output.
   If the tool says there is no date filter, do NOT invent a year such as
   2024-2025, 2025, 2026, etc.

6. Do NOT call a value USD, dollars, or "$" unless the tool explicitly says
   that currency. For these Skylark datasets, prefer ₹ when presenting
   monetary values because the source fields are rupee-denominated, unless
   the tool output explicitly indicates otherwise.

7. Do NOT convert rupees into millions, crores, dollars or any other unit
   unless the conversion is directly supported by the numeric result and
   clearly stated. Prefer the raw ₹ value when in doubt.

8. "Active work orders" means exactly the definition returned by the tool.
   Do not silently substitute another status.

9. "Stuck", "Pause / struck", "Update Required", "Not Started", "Open",
   "Won", "Dead", etc. are source terminology. Do not create synonyms such
   as "blocked", "closed-lost", "inactive", "delayed", or "at risk" unless
   the source data itself supports that interpretation.

10. Missing/non-numeric values must never be treated as zero without saying
    so. Use the populated-row counts and missing-row counts supplied by the
    tool.

11. Do not claim that a field is complete, reliable, clean or accurate unless
    the returned data supports that statement.

12. If a metric is unavailable or null, say that it is unavailable rather
    than filling in a guess.

13. Do not add unsupported causal explanations such as "cash-flow impact",
    "resource constraints", "sales weakness", etc. unless the data directly
    supports the statement.

14. Do not fabricate a "biggest at-risk deal". You may mention the
    highest-value open deals returned by the tool, but describe them only
    using fields actually returned.

15. Do not fabricate aging. No receivable aging analysis is available unless
    explicitly returned by the tool.

OUTPUT FORMAT:

Produce EXACTLY these sections:

## Headline Metrics

3-5 concise bullets covering:
- Open pipeline value
- Won deal value
- Active work orders
- Billed value
- Collected value
- Receivable value

Only include metrics for which the tool returned actual values.

## Sector Performance

Use a markdown table with:

| Sector | Open Pipeline Value | Won Value | Active Work Orders |

Include every sector returned by the tool.

Do not invent sectors.

## Pipeline Health

2-4 bullets covering only what the tool returned:
- open deal count
- stage distribution
- highest-value open deals
- meaningful concentration visible in the returned data

Do not invent movement, dates, aging or probabilities.

## Execution & Collections Watch

2-4 bullets covering:
- execution statuses explicitly flagged by the tool
- Billing Status = Update Required count
- receivable value

Do not invent additional statuses.

## Data Caveats

Always include this section.

Use the returned data-quality information and coverage figures.

Mention:
- missing percentages when available
- populated versus total rows
- missing/non-numeric deal values
- missing/non-numeric financial values
- any data-quality notes explicitly returned

Do not invent additional data-quality problems.

STYLE:

- Executive-friendly.
- Short.
- No unsupported claims.
- No fake dates.
- No fake terminology.
- No placeholders.
- No "as of today" unless the tool explicitly provides today's date.
- No "2024-2025" or similar period unless the tool explicitly provides it.
- No dollar signs unless explicitly supported.
- Use ₹ for the rupee-denominated Skylark financial fields.
"""


def _convert_tool_schema(schema: dict) -> dict:
    """Convert project tool schema into Groq function format."""

    parameters = schema.get(
        "parameters",
        schema.get("input_schema", {}),
    )

    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get(
                "description",
                "",
            ),
            "parameters": parameters,
        },
    }


def _build_groq_tools() -> list[dict]:
    return [
        _convert_tool_schema(schema)
        for schema in TOOL_SCHEMAS
    ]


class LeadershipReportGenerator:

    def __init__(self):

        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set in backend/.env"
            )

        self.model = os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-20b",
        )

        self.client = Groq(
            api_key=api_key
        )

        self.store = DataStore()

        self.runner = ToolRunner(
            self.store
        )

        self.groq_tools = _build_groq_tools()

    def generate(
        self,
        focus: str | None = None,
        max_tool_iterations: int = 4,
    ) -> dict:
        """
        Generate the leadership report.

        Normally this should require only one or two tool calls:
            1. leadership_summary
            2. optional get_data_quality_notes

        A small budget is intentional to avoid unnecessary Groq usage.
        """

        user_msg = (
            "Generate the leadership update using the "
            "leadership_summary tool."
        )

        if focus:
            user_msg += (
                " Additional user focus: "
                f"{focus}"
            )

        messages = [
            {
                "role": "system",
                "content": LEADERSHIP_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_msg,
            },
        ]

        tool_trace = []

        for _ in range(max_tool_iterations):

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=self.groq_tools,
                    tool_choice="auto",
                    temperature=0.1,
                    max_tokens=2500,
                )

            except Exception as exc:
                return {
                    "report_markdown": (
                        "I couldn't generate the leadership report "
                        f"because the AI model returned an error: {exc}"
                    ),
                    "tool_trace": tool_trace,
                }

            if not response.choices:
                return {
                    "report_markdown": (
                        "The AI model returned no response. "
                        "Please try again."
                    ),
                    "tool_trace": tool_trace,
                }

            message = response.choices[0].message

            # ----------------------------------------------------------
            # Final report
            # ----------------------------------------------------------

            if not message.tool_calls:

                final_text = (
                    message.content
                    or ""
                ).strip()

                if not final_text:
                    return {
                        "report_markdown": (
                            "The AI model returned an empty leadership "
                            "report. Please try again."
                        ),
                        "tool_trace": tool_trace,
                    }

                return {
                    "report_markdown": final_text,
                    "tool_trace": tool_trace,
                }

            # ----------------------------------------------------------
            # Preserve assistant tool calls
            # ----------------------------------------------------------

            assistant_message = {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [],
            }

            for tool_call in message.tool_calls:

                assistant_message["tool_calls"].append(
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": (
                                tool_call.function.arguments
                                or "{}"
                            ),
                        },
                    }
                )

            messages.append(
                assistant_message
            )

            # ----------------------------------------------------------
            # Execute tools
            # ----------------------------------------------------------

            for tool_call in message.tool_calls:

                tool_name = tool_call.function.name

                raw_arguments = (
                    tool_call.function.arguments
                    or "{}"
                )

                try:

                    tool_args = json.loads(
                        raw_arguments
                    )

                    if not isinstance(
                        tool_args,
                        dict,
                    ):
                        raise ValueError(
                            "Tool arguments must be a JSON object."
                        )

                except (
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                ) as exc:

                    output = (
                        "Invalid JSON tool arguments: "
                        f"{exc}"
                    )

                    is_error = True
                    tool_args = {}

                else:

                    try:

                        output = self.runner.execute(
                            tool_name,
                            tool_args,
                        )

                        is_error = False

                    except ToolExecutionError as exc:

                        output = str(exc)

                        is_error = True

                    except Exception as exc:

                        output = (
                            "Unexpected tool error: "
                            f"{exc}"
                        )

                        is_error = True

                tool_trace.append(
                    {
                        "tool": tool_name,
                        "input": tool_args,
                        "error": is_error,
                    }
                )

                if isinstance(
                    output,
                    (dict, list),
                ):

                    tool_content = json.dumps(
                        output,
                        default=str,
                    )

                else:

                    tool_content = str(
                        output
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_content,
                    }
                )

        return {
            "report_markdown": (
                "I couldn't complete the leadership report "
                "within the available analysis budget. "
                "No unsupported figures were added."
            ),
            "tool_trace": tool_trace,
        }