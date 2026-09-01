import uuid
import datetime
import asyncio
from typing import Any, Mapping
from ...domain.workflow import Workflow, WorkflowStepResult
from ...domain.execution import ExecutionRequest
from ..dispatcher import Dispatcher

class WorkflowEngine:
    def __init__(self, dispatcher: Dispatcher, state_store: Any):
        self.dispatcher = dispatcher
        self.state_store = state_store

    def plan(self, workflow: Workflow, request: ExecutionRequest) -> list[dict]:
        steps = []
        for step_idx, skill_name in enumerate(workflow.steps):
            try:
                decision = self.dispatcher.route(
                    ExecutionRequest(
                        skill=skill_name,
                        input=request.input,
                        caller=request.caller,
                        session_id=request.session_id,
                        project_id=request.project_id,
                        depth=request.depth,
                    )
                )
                if decision.worker is None:
                    steps.append({
                        "step": step_idx + 1,
                        "skill": decision.skill.name,
                        "worker": "UNKNOWN",
                        "provider": "UNKNOWN",
                        "reason": decision.reason,
                        "rejections": decision.rejections,
                        "error_code": decision.error_code,
                        "status": f"error: {decision.error_code or 'ROUTING_FAILED'}",
                    })
                else:
                    steps.append({
                        "step": step_idx + 1,
                        "skill": decision.skill.name,
                        "worker": decision.worker.name,
                        "provider": decision.worker.provider,
                        "reason": decision.reason,
                        "rejections": decision.rejections,
                        "status": "planned",
                    })
            except Exception as e:
                steps.append({
                    "step": step_idx + 1,
                    "skill": skill_name,
                    "worker": "UNKNOWN",
                    "provider": "UNKNOWN",
                    "status": f"error: {str(e)}"
                })
        return steps

    def _step_input(
        self,
        workflow: Workflow,
        state: dict[str, Any],
        step_idx: int,
        request: ExecutionRequest,
        parallel: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        skill_name = workflow.steps[step_idx]
        mapping = workflow.mappings.get(skill_name)
        if mapping is None:
            values = dict(request.input) if step_idx == 0 else {}
            return self._add_resume_input(state, step_idx, values), {}

        context = {"input": request.input, **state.get("context", {})}
        values: dict[str, Any] = {}
        for destination, source in mapping.items():
            current: Any = context
            for part in source.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    raise ValueError(
                        f"Workflow step '{skill_name}' cannot resolve context value '{source}'"
                    )
                current = current[part]
            values[destination] = current
        return self._add_resume_input(state, step_idx, values), dict(mapping)

    @staticmethod
    def _add_resume_input(
        state: dict[str, Any], step_idx: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        pause = state.get("pause")
        if not isinstance(pause, Mapping) or pause.get("step") != step_idx + 1:
            return values
        answers = state.get("answers", {})
        if answers:
            values["_harness_answers"] = dict(answers)
        continuation_token = pause.get("continuation_token")
        if isinstance(continuation_token, str) and continuation_token:
            values["_harness_continuation_token"] = continuation_token
        return values

    @staticmethod
    def _record_context(
        state: dict[str, Any],
        skill_name: str,
        output: dict[str, Any] | None,
    ) -> None:
        state.setdefault("context", {})[skill_name] = {"output": output or {}}

    @staticmethod
    def _store_result(state: dict[str, Any], result: WorkflowStepResult) -> None:
        state["results"] = [
            existing for existing in state.get("results", [])
            if existing.get("step") != result.get("step")
        ]
        state["results"].append(result)

    async def run(
        self,
        workflow: Workflow,
        request: ExecutionRequest,
        execution_id: str | None = None,
        answers: Mapping[str, Any] | None = None,
    ) -> str:
        if not execution_id:
            execution_id = str(uuid.uuid4())
            state = {
                "workflow_name": workflow.name,
                "status": "RUNNING",
                "current_step": 0,
                "results": [],
                "context_version": 1,
                "context": {},
                "input": dict(request.input),
                "caller": request.caller,
                "session_id": request.session_id,
                "project_id": request.project_id,
                "depth": request.depth,
                "cancel_requested": False,
                "answers": {},
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            await self.state_store.save_workflow_execution(execution_id, state)
        else:
            state = await self.state_store.get_workflow_execution(execution_id)
            if not state:
                raise ValueError(f"Workflow execution {execution_id} not found")
            if state["status"] == "COMPLETED":
                print("Workflow already completed.")
                return execution_id
            if answers:
                stored_answers = state.setdefault("answers", {})
                stored_answers.update(dict(answers))
            if not request.input and state.get("input"):
                request = ExecutionRequest(
                    skill=request.skill,
                    input=state["input"],
                    caller=state.get("caller", request.caller),
                    session_id=state.get("session_id", request.session_id),
                    project_id=state.get("project_id", request.project_id),
                    depth=state.get("depth", request.depth),
                )
            state["status"] = "RUNNING"
            state["cancel_requested"] = False
            await self.state_store.save_workflow_execution(execution_id, state)

        steps = workflow.steps

        if workflow.parallel and state["current_step"] == 0:
            successful_steps = {
                item.get("step") for item in state.get("results", [])
                if item.get("status") == "SUCCESS"
            }
            pending = [
                index for index in range(len(steps)) if index + 1 not in successful_steps
            ]

            async def run_parallel_step(step_idx: int) -> WorkflowStepResult:
                skill_name = steps[step_idx]
                try:
                    step_input, lineage = self._step_input(
                        workflow, state, step_idx, request, parallel=True
                    )
                    result = await self.dispatcher.dispatch(ExecutionRequest(
                        skill=skill_name,
                        input=step_input,
                        caller=request.caller,
                        session_id=request.session_id,
                        project_id=request.project_id,
                        depth=request.depth,
                    ))
                    return {
                        "step": step_idx + 1,
                        "skill": skill_name,
                        "execution_id": result.execution_id,
                        "status": result.status,
                        "input": step_input,
                        "lineage": lineage,
                        "output": result.output,
                        "error": result.error,
                        "error_details": result.error_details,
                    }
                except Exception as error:
                    return {
                        "step": step_idx + 1,
                        "skill": skill_name,
                        "execution_id": "",
                        "status": "FAILED",
                        "input": {},
                        "lineage": {},
                        "error": str(error),
                        "error_details": None,
                    }

            results = await asyncio.gather(*(run_parallel_step(index) for index in pending))
            for result in results:
                self._store_result(state, result)
                if result["status"] == "SUCCESS":
                    self._record_context(state, result["skill"], result.get("output"))
            if len(state.get("results", [])) == len(steps) and all(
                item["status"] == "SUCCESS" for item in state["results"]
            ):
                state["current_step"] = len(steps)
                state["status"] = "COMPLETED"
                print("Workflow completed.")
            else:
                state["status"] = "FAILED"
                for item in state["results"]:
                    if item["status"] != "SUCCESS" and item.get("error"):
                        print(f"      [FAIL] {item.get('error_details') or item['error']}")
            await self.state_store.save_workflow_execution(execution_id, state)
            return execution_id

        while state["current_step"] < len(steps):
            step_idx = state["current_step"]
            skill_name = steps[step_idx]
            if state.get("cancel_requested") or state.get("status") == "CANCELLED":
                state["status"] = "CANCELLED"
                await self.state_store.save_workflow_execution(execution_id, state)
                print("Workflow cancelled.")
                break
            try:
                step_input, lineage = self._step_input(workflow, state, step_idx, request)
            except ValueError as error:
                print(f"      [FAIL] {error}\n")
                state["status"] = "FAILED"
                self._store_result(state, {
                    "step": step_idx + 1,
                    "skill": skill_name,
                    "execution_id": "",
                    "status": "FAILED",
                    "input": {},
                    "lineage": {},
                    "error": str(error),
                    "error_details": None,
                })
                await self.state_store.save_workflow_execution(execution_id, state)
                break

            try:
                decision = self.dispatcher.route(
                    ExecutionRequest(
                        skill=skill_name,
                        input=step_input,
                        caller=request.caller,
                        session_id=request.session_id,
                        project_id=request.project_id,
                        depth=request.depth,
                    )
                )
                provider_display = decision.worker.provider
            except Exception:
                provider_display = "unknown"

            print(f"[{step_idx + 1}/{len(steps)}] {skill_name.ljust(20)} -> {provider_display}")

            step_request = ExecutionRequest(
                skill=skill_name,
                input=step_input,
                caller=request.caller,
                session_id=request.session_id,
                project_id=request.project_id,
                depth=request.depth,
            )

            try:
                result = await self.dispatcher.dispatch(step_request)
                if result.status == "SUCCESS":
                    print(f"      [OK] completed\n")
                    self._store_result(state, {
                        "step": step_idx + 1,
                        "skill": skill_name,
                        "execution_id": result.execution_id,
                        "status": "SUCCESS",
                        "input": step_input,
                        "lineage": lineage,
                        "output": result.output,
                    })
                    self._record_context(state, skill_name, result.output)
                    state["current_step"] += 1
                    state.pop("pause", None)
                    await self.state_store.save_workflow_execution(execution_id, state)
                elif result.status == "PAUSED":
                    pause_output = result.output if isinstance(result.output, Mapping) else {}
                    state["status"] = "PAUSED"
                    state["pause"] = {
                        "step": step_idx + 1,
                        "skill": skill_name,
                        "questions": list(pause_output.get("questions", [])),
                        "continuation_token": pause_output.get("continuation_token"),
                    }
                    self._store_result(state, {
                        "step": step_idx + 1,
                        "skill": skill_name,
                        "execution_id": result.execution_id,
                        "status": "PAUSED",
                        "input": step_input,
                        "lineage": lineage,
                        "output": result.output,
                    })
                    await self.state_store.save_workflow_execution(execution_id, state)
                    print("      [PAUSED] waiting for user answers\n")
                    break
                else:
                    print(f"      [FAIL] failed\n")
                    if result.error:
                        print(f"      [ERROR] {result.error}")
                    state["status"] = "FAILED"
                    self._store_result(state, {
                        "step": step_idx + 1,
                        "skill": skill_name,
                        "execution_id": result.execution_id,
                        "status": "FAILED",
                        "input": step_input,
                        "lineage": lineage,
                        "error": result.error,
                        "error_details": result.error_details,
                    })
                    await self.state_store.save_workflow_execution(execution_id, state)
                    break
            except Exception as e:
                print(f"      [FAIL] failed with exception: {e}\n")
                state["status"] = "FAILED"
                self._store_result(state, {
                    "step": step_idx + 1,
                    "skill": skill_name,
                    "execution_id": "",
                    "status": "FAILED",
                    "input": step_input,
                    "lineage": lineage,
                    "error": str(e),
                    "error_details": None,
                })
                await self.state_store.save_workflow_execution(execution_id, state)
                break

        if state["current_step"] == len(steps):
            state["status"] = "COMPLETED"
            await self.state_store.save_workflow_execution(execution_id, state)
            print("Workflow completed.")

        return execution_id
