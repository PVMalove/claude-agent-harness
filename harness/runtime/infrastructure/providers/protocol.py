from collections.abc import Mapping
from typing import Any


JSONRPC_VERSION = "2.0"
PROTOCOL = "harness.provider"
PROTOCOL_VERSION = 1
EXECUTE_METHOD = "execute"
ASK_USER_QUESTION_METHOD = "AskUserQuestion"


class ProtocolError(ValueError):
    """The provider sent a message that is not part of the provider protocol."""


def execution_request(
    execution_id: str,
    skill: str,
    input_data: Mapping[str, Any],
    capabilities: set[str],
) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": execution_id,
        "method": EXECUTE_METHOD,
        "params": {
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "execution_id": execution_id,
            "skill": skill,
            "input": dict(input_data),
            "capabilities": sorted(capabilities),
        },
    }


def question_response(request_id: Any, answers: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": {
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "answers": dict(answers),
        },
    }


def question_params(message: Mapping[str, Any]) -> tuple[Any, list[Mapping[str, Any]]]:
    _validate_jsonrpc(message)
    if message.get("method") != ASK_USER_QUESTION_METHOD:
        raise ProtocolError(f"unsupported provider method: {message.get('method')!r}")

    params = message.get("params")
    if not isinstance(params, Mapping):
        raise ProtocolError("AskUserQuestion params must be an object")
    _validate_version(params)

    questions = params.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ProtocolError("AskUserQuestion params.questions must be a non-empty list")
    if not all(isinstance(question, Mapping) for question in questions):
        raise ProtocolError("AskUserQuestion questions must be objects")
    validate_questions(questions)
    return message.get("id"), questions


def execution_result(
    message: Mapping[str, Any], execution_id: str
) -> tuple[str, dict[str, Any] | None, str | None]:
    _validate_jsonrpc(message)
    if message.get("id") != execution_id:
        raise ProtocolError("terminal result id does not match the execution request")

    result = message.get("result")
    if not isinstance(result, Mapping):
        raise ProtocolError("terminal result must contain a result object")
    _validate_version(result)

    status = result.get("status")
    if status not in {"SUCCESS", "FAILED", "PAUSED"}:
        raise ProtocolError("terminal result status must be SUCCESS, FAILED, or PAUSED")

    output = result.get("output", {})
    if output is None:
        output = {}
    if not isinstance(output, Mapping):
        raise ProtocolError("terminal result output must be an object")
    if status == "PAUSED":
        questions = output.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ProtocolError("PAUSED result output must contain questions")
        if not all(isinstance(question, Mapping) for question in questions):
            raise ProtocolError("PAUSED result questions must be objects")
        validate_questions(questions)

    error = result.get("error")
    if error is not None and not isinstance(error, str):
        raise ProtocolError("terminal result error must be a string")
    if status == "FAILED" and not error:
        error = "Provider reported FAILED without an error"
    return status, dict(output), error


def _validate_jsonrpc(message: Mapping[str, Any]) -> None:
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError("provider message must use JSON-RPC 2.0")
    if "id" not in message or message.get("id") is None:
        raise ProtocolError("provider message is missing id")


def validate_questions(questions: list[Mapping[str, Any]]) -> None:
    question_ids: set[str] = set()
    for question in questions:
        question_id = question.get("id")
        if (
            not isinstance(question_id, str)
            or not question_id.strip()
            or question_id in question_ids
        ):
            raise ProtocolError(
                "AskUserQuestion questions must have unique non-empty question ids"
            )
        question_ids.add(question_id)

        prompt = question.get("question")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProtocolError(
                f"AskUserQuestion question {question_id!r} must have a non-empty question"
            )

        options = question.get("options")
        if options is None or options == []:
            if "multiSelect" in question and question["multiSelect"] is not False:
                raise ProtocolError(
                    f"Open question {question_id!r} must use single-select mode"
                )
            continue

        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ProtocolError(
                f"Structured question {question_id!r} must declare 2 to 4 options"
            )
        if not all(isinstance(option, str) and option.strip() for option in options):
            raise ProtocolError(
                f"Structured question {question_id!r} options must be non-empty strings"
            )
        if len(set(options)) != len(options):
            raise ProtocolError(
                f"Structured question {question_id!r} options must be unique"
            )

        header = question.get("header")
        if not isinstance(header, str) or not 0 < len(header.strip()) <= 12:
            raise ProtocolError(
                f"Structured question {question_id!r} must have a short header"
            )
        if question.get("multiSelect", False) is not False:
            raise ProtocolError(
                f"Structured question {question_id!r} must be single-select"
            )
        if "(Recommended)" not in options[0]:
            raise ProtocolError(
                f"Structured question {question_id!r} must put the recommended option first"
            )
        if any("(Recommended)" in option for option in options[1:]):
            raise ProtocolError(
                f"Structured question {question_id!r} has a non-leading recommendation"
            )


def _validate_version(message: Mapping[str, Any]) -> None:
    if message.get("protocol") != PROTOCOL or message.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"provider message must use {PROTOCOL} protocol version {PROTOCOL_VERSION}"
        )
