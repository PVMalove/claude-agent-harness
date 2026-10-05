# Technical English for agent coordination

Agents must read this shared contract before their first English handoff.
This is the managed source for technical-English coordination in an installed project.
It is based on the principles of ASD-STE100 and does not claim conformance to that standard.
A project with its own existing `AGENTS.md` keeps that file unchanged; it must link to this
contract itself for the rule to apply there.

## Scope, language, and authority

This contract applies to English agent-to-agent coordination in every standard installation,
including projects that do not select `backend-orchestration`.
It covers handoff notes, checkpoints, state evidence, dependency explanations, messages to the
next worker, and free coordination fields that the existing protocol requires in English.
Coordinators and workers use the same source before the first English handoff.

Follow the project's existing language policy for user-facing replies and completion reports.
In backend orchestration, completion reports addressed to the coordinator remain Russian and
contain `"report_language": "ru"`.
Exact tokens and source quotations keep their original language.
This writing contract changes neither authority nor scope, evidence binding, or human approval.
The immutable brief and the existing role, project, and protocol contracts retain their authority.

## Clear instructions without changing meaning

Use short sentences as guidance, not a word-count limit.
Put one instruction in each sentence.
Use active voice.
Name the actor in every instruction; omit the actor only when the source makes it unambiguous.
Put necessary conditions before the action they control.
Use one established project term for each concept.
Keep facts, assumptions, and requested actions distinguishable in ordinary prose.
Label an assumption when the reader could mistake it for a verified fact.
No fixed message template is required.

Meaning takes priority over brevity.
Preserve every material condition, negation, prohibition, and limit on scope.
Preserve modality: `must` is an obligation, `must not` is a prohibition, and `may` is permission
or possibility as used in the source.
Keep uncertainty and unknown results explicit.
If a requirement is ambiguous, escalate the ambiguity to the coordinator or requirement owner.
Do not choose an interpretation or turn an assumption into authority.

## Exact tokens and source fidelity

Preserve JSON field names, enums, commands, paths, SHA, test names, code, and quotations of
source evidence verbatim.
Explain a token in surrounding prose when needed; keep the token itself unchanged.
A paraphrase must preserve the source's conditions, uncertainty, and limits.
Do not strengthen a claim, omit a limit, or present a paraphrase as an exact quotation.

Before sending English coordination text, silently check:

- Is the recipient clear, and is the requested action clear?
- Are the necessary conditions and prohibitions preserved?
- Are scope, modality, and uncertainty unchanged?
- Are exact tokens, quoted evidence, and paraphrased conditions faithful to the source?

Revise the text until these checks pass.
This check creates no extra message, JSON field, or mandatory report for a short request.

## Examples and counterexamples

These are illustrative messages, not additional workflow permissions.

| Source meaning | Clear English | Counterexample and lost meaning |
| --- | --- | --- |
| QA evidence must cover the candidate SHA, and opening a PR needs explicit human approval. | If accepted QA evidence covers candidate SHA `0123456789abcdef0123456789abcdef01234567`, and the developer explicitly approves opening the PR, the coordinator may start the separate PR step for that candidate. | "QA passed. Open the PR." drops the candidate binding and human approval, and turns permission into an order. |
| Workers are prohibited from editing the immutable brief. | Workers must not edit the immutable brief. If scope must change, workers must escalate to the coordinator. | "Workers should avoid editing the brief." weakens a prohibition into advice. |
| A runtime may provide telemetry; the result is unknown for this dispatch. | The runtime may provide token telemetry. Its availability is unknown for this dispatch. | "The runtime will provide telemetry." turns an uncertain possibility into a promise. |
| The requested command is `make lint`; the completion field is `"report_language": "ru"`. | Run `make lint`. Keep `"report_language": "ru"` in the completion report. | "Run the lint command and set the language to Russian." loses the exact command, field name, and enum value. |

During review, compare the message with its source.
Check that every material condition, prohibition, scope limit, modality, and uncertainty survives.
Check exact commands, identifiers, paths, SHA, test names, code, and quoted evidence verbatim.
A shorter message passes review only when it preserves that meaning and those tokens.
