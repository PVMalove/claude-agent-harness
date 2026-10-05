# Technical English for agent coordination

Agents must read this shared contract before their first English handoff.
This is the managed source for technical-English coordination in an installed project.

## Clear instructions without changing meaning

Use short sentences as guidance, not a word-count limit.
Put one instruction in each sentence.
Use active voice.
Name the actor when responsibility could be unclear.
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
- Are exact tokens and quoted evidence faithful to the source?

Revise the text until these checks pass.
This check creates no extra message, JSON field, or mandatory report for a short request.
