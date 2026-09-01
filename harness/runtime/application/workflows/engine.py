import uuid
import datetime
from typing import Any
from ...domain.workflow import Workflow
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
                skill = self.dispatcher.skills.resolve(skill_name)
                candidates = self.dispatcher.resolver.resolve(skill.requirements)
                authorized = self.dispatcher.policy.authorize(request, candidates)
                healthy = self.dispatcher.health.filter(authorized)
                worker = self.dispatcher.scheduler.select(skill, healthy)

                steps.append({
                    "step": step_idx + 1,
                    "skill": skill.name,
                    "worker": worker.name,
                    "provider": worker.provider,
                    "status": "planned"
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

    async def run(self, workflow: Workflow, request: ExecutionRequest, execution_id: str | None = None) -> str:
        if not execution_id:
            execution_id = str(uuid.uuid4())
            state = {
                "workflow_name": workflow.name,
                "status": "RUNNING",
                "current_step": 0,
                "results": [],
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

        steps = workflow.steps

        while state["current_step"] < len(steps):
            step_idx = state["current_step"]
            skill_name = steps[step_idx]

            try:
                # We need to know provider for UX print "-> claude"
                skill = self.dispatcher.skills.resolve(skill_name)
                candidates = self.dispatcher.resolver.resolve(skill.requirements)
                authorized = self.dispatcher.policy.authorize(request, candidates)
                healthy = self.dispatcher.health.filter(authorized)
                worker = self.dispatcher.scheduler.select(skill, healthy)
                provider_display = worker.provider
            except:
                provider_display = "unknown"

            print(f"[{step_idx + 1}/{len(steps)}] {skill_name.ljust(20)} -> {provider_display}")

            step_request = ExecutionRequest(
                skill=skill_name,
                input=request.input,
                caller=request.caller,
                session_id=request.session_id,
                project_id=request.project_id
            )

            try:
                result = await self.dispatcher.dispatch(step_request)
                if result.status == "SUCCESS":
                    print(f"      [OK] completed\n")
                    state["results"].append({
                        "skill": skill_name,
                        "status": "SUCCESS",
                        "output": result.output
                    })
                    state["current_step"] += 1
                    await self.state_store.save_workflow_execution(execution_id, state)
                else:
                    print(f"      [FAIL] failed\n")
                    state["status"] = "FAILED"
                    state["results"].append({
                        "skill": skill_name,
                        "status": "FAILED",
                        "error": result.error
                    })
                    await self.state_store.save_workflow_execution(execution_id, state)
                    break
            except Exception as e:
                print(f"      [FAIL] failed with exception: {e}\n")
                state["status"] = "FAILED"
                state["results"].append({
                    "skill": skill_name,
                    "status": "FAILED",
                    "error": str(e)
                })
                await self.state_store.save_workflow_execution(execution_id, state)
                break

        if state["current_step"] == len(steps):
            state["status"] = "COMPLETED"
            await self.state_store.save_workflow_execution(execution_id, state)
            print("Workflow completed.")

        return execution_id
