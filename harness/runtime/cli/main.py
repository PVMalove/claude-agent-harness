import argparse
import asyncio
import json
import sys
from pathlib import Path

from ..domain.skill import Skill
from ..domain.worker import Worker
from ..domain.workflow import Workflow
from ..domain.policy import ExecutionPolicy
from ..domain.execution import ExecutionRequest

from ..application.registry import ProviderRegistry, SkillRegistry
from ..application.routing.resolver import CapabilityResolver
from ..application.routing.policy import PolicyEngine
from ..application.routing.health import HealthRegistry
from ..application.routing.scheduler import Scheduler
from ..application.execution.planner import Planner
from ..application.execution.executor import Executor
from ..application.dispatcher import Dispatcher
from ..application.workflows.engine import WorkflowEngine

from ..infrastructure.providers.cli import CLIProvider
from ..infrastructure.providers.mcp import MCPProvider
from ..infrastructure.config import ProviderConfig, load_config
from ..infrastructure.events.bus import EventBus
from ..infrastructure.state.sqlite import SQLiteStateStore

def setup_components(repo_path: Path):
    config_path = repo_path / ".harness" / "orchestration.toml"
    config = load_config(config_path)

    skills = {
        name: Skill(
            name=name,
            requirements=set(data.requirements),
            execution_policy=ExecutionPolicy(preferred=list(data.execution.preferred)),
        )
        for name, data in config.skills.items()
    }

    workers = {
        name: Worker(
            name=name,
            provider=data.provider,
            capabilities=set(data.capabilities),
            priority=data.priority,
            health=data.health,
        )
        for name, data in config.workers.items()
    }

    workflows = {
        name: Workflow(name=name, steps=list(data.steps), parallel=data.parallel)
        for name, data in config.workflows.items()
    }

    provider_registry = ProviderRegistry()
    for provider_name, provider_config in config.providers.items():
        provider_registry.register(
            provider_name,
            _build_provider(provider_name, provider_config, config.default_timeout, repo_path),
        )

    state_store = SQLiteStateStore()
    event_bus = EventBus(state_store)

    skill_registry = SkillRegistry(skills)
    resolver = CapabilityResolver(workers)
    policy_engine = PolicyEngine(
        delegation=dict(config.delegation),
        max_depth=config.max_depth,
    )
    health_registry = HealthRegistry(
        {name: data.health for name, data in config.workers.items()}
    )
    scheduler = Scheduler()
    planner = Planner(provider_registry)
    executor = Executor(provider_registry)

    dispatcher = Dispatcher(
        skills=skill_registry,
        policy=policy_engine,
        resolver=resolver,
        health=health_registry,
        scheduler=scheduler,
        planner=planner,
        executor=executor,
        events=event_bus,
        max_parallel=config.max_parallel,
    )

    workflow_engine = WorkflowEngine(dispatcher, state_store)

    return {
        "skills": skills,
        "workers": workers,
        "workflows": workflows,
        "provider_registry": provider_registry,
        "skill_registry": skill_registry,
        "resolver": resolver,
        "scheduler": scheduler,
        "dispatcher": dispatcher,
        "workflow_engine": workflow_engine
    }


def _build_provider(
    name: str, config: ProviderConfig, default_timeout: float = 600.0, cwd: Path | None = None
):
    if config.type == "cli":
        if not config.command:
            raise ValueError(f"Provider '{name}' of type 'cli' is missing 'command'")
        timeout = config.timeout if config.timeout is not None else default_timeout
        return CLIProvider(
            config.command,
            list(config.args),
            timeout=timeout,
            cwd=cwd,
            retry_policy=config.retry_policy,
        )
    if config.type == "mcp":
        timeout = config.timeout if config.timeout is not None else default_timeout
        return MCPProvider(timeout=timeout, retry_policy=config.retry_policy)
    raise ValueError(f"Provider '{name}' has unsupported type '{config.type}'")

def main():
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("--repo", default=".", help="Repository path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # WORKFLOW commands
    wf_parser = subparsers.add_parser("workflow")
    wf_subparsers = wf_parser.add_subparsers(dest="wf_command", required=True)

    wf_list = wf_subparsers.add_parser("list")
    wf_show = wf_subparsers.add_parser("show")
    wf_show.add_argument("name")

    wf_plan = wf_subparsers.add_parser("plan")
    wf_plan.add_argument("name")
    wf_plan.add_argument("--caller", default="USER")
    wf_plan.add_argument("--depth", type=int, default=0)

    wf_run = wf_subparsers.add_parser("run")
    wf_run.add_argument("name")
    wf_run.add_argument("--input", default="{}")
    wf_run.add_argument("--caller", default="USER")
    wf_run.add_argument("--depth", type=int, default=0)

    wf_status = wf_subparsers.add_parser("status")
    wf_status.add_argument("id")

    wf_resume = wf_subparsers.add_parser("resume")
    wf_resume.add_argument("id")

    wf_cancel = wf_subparsers.add_parser("cancel")
    wf_cancel.add_argument("id")

    # SKILL commands
    sk_parser = subparsers.add_parser("skill")
    sk_subparsers = sk_parser.add_subparsers(dest="sk_command", required=True)
    sk_list = sk_subparsers.add_parser("list")
    sk_explain = sk_subparsers.add_parser("explain")
    sk_explain.add_argument("name")
    sk_explain.add_argument("--caller", default="USER")
    sk_explain.add_argument("--depth", type=int, default=0)
    sk_run = sk_subparsers.add_parser("run")
    sk_run.add_argument("name")
    sk_run.add_argument("--input", default="{}")
    sk_run.add_argument("--caller", default="USER")
    sk_run.add_argument("--depth", type=int, default=0)

    # PROVIDER and WORKER commands
    prov_parser = subparsers.add_parser("provider")
    prov_subparsers = prov_parser.add_subparsers(dest="prov_command", required=True)
    prov_subparsers.add_parser("list")

    wrk_parser = subparsers.add_parser("worker")
    wrk_subparsers = wrk_parser.add_subparsers(dest="wrk_command", required=True)
    wrk_subparsers.add_parser("list")

    args = parser.parse_args()
    try:
        comps = setup_components(Path(args.repo))
    except (FileNotFoundError, ValueError) as error:
        print(error)
        return 1

    def get_req(input_str="{}", *, caller="USER", depth=0):
        try:
            return ExecutionRequest(
                skill="", input=json.loads(input_str), caller=caller, depth=depth
            )
        except:
            return ExecutionRequest(skill="", caller=caller, depth=depth)

    # Workflow Actions
    if args.command == "workflow":
        wf_engine = comps["workflow_engine"]
        workflows = comps["workflows"]

        if args.wf_command == "list":
            print("Available workflows:\n")
            for w in workflows:
                print(w)
            return 0

        if args.wf_command == "show":
            wf = workflows.get(args.name)
            if not wf:
                print("Workflow not found")
                return 1
            print(f"{wf.name}\n")
            for i, step in enumerate(wf.steps):
                print(f"{i+1}. {step}")
            return 0

        if args.wf_command == "plan":
            wf = workflows.get(args.name)
            if not wf:
                print("Workflow not found")
                return 1
            steps = wf_engine.plan(
                wf,
                get_req(caller=args.caller, depth=args.depth),
            )
            print(f"Workflow: {wf.name}\n")
            print(f"{'STEP'.ljust(20)} {'SKILL'.ljust(17)} {'WORKER'.ljust(12)} PROVIDER")
            print("-" * 61)
            for s in steps:
                print(f"{str(s['step']).ljust(20)} {s['skill'].ljust(17)} {s['worker'].ljust(12)} {s['provider']}")
                if s["status"] == "planned":
                    print(f"  reason: {s['reason']}")
                    for worker, rejection in s["rejections"].items():
                        print(f"  rejected {worker}: {rejection}")
                else:
                    print(f"  reason: {s.get('reason', s['status'])}")
                    if s.get("error_code"):
                        print(f"  error: {s['error_code']}")
                    for worker, rejection in s.get("rejections", {}).items():
                        print(f"  rejected {worker}: {rejection}")
            print("\nRouting is declarative.")
            return 1 if any(step["status"].startswith("error:") for step in steps) else 0

        if args.wf_command == "run":
            wf = workflows.get(args.name)
            if not wf:
                print("Workflow not found")
                return 1
            req = get_req(args.input, caller=args.caller, depth=args.depth)
            execution_id = asyncio.run(wf_engine.run(wf, req))
            state = asyncio.run(
                comps["dispatcher"].events.state_store.get_workflow_execution(execution_id)
            )
            return 0 if state and state["status"] == "COMPLETED" else 1

        if args.wf_command == "resume":
            # For resume we need to fetch state to know which workflow it is.
            state = asyncio.run(comps["dispatcher"].events.state_store.get_workflow_execution(args.id))
            if not state:
                print("Workflow execution not found")
                return 1
            wf = workflows.get(state["workflow_name"])
            if not wf:
                print("Workflow definition not found")
                return 1
            asyncio.run(wf_engine.run(wf, get_req(), args.id))
            return 0

        # ... (other wf commands omitted for brevity but can be added similarly)

    # Skill Actions
    elif args.command == "skill":
        if args.sk_command == "list":
            for s in comps["skills"]: print(s)
            return 0

        if args.sk_command == "explain":
            decision = comps["dispatcher"].route(
                ExecutionRequest(skill=args.name, caller=args.caller, depth=args.depth)
            )
            print(f"Skill: {decision.skill.name}\n\nRequirements:")
            for req in decision.skill.requirements:
                print(f"  * {req}")

            print("\nCandidates:")
            workers = comps["workers"]

            for name, worker in workers.items():
                rejection = decision.rejections.get(name)
                if rejection:
                    print(f"{name}: rejected; {rejection}")
                else:
                    matched_caps = len(
                        decision.skill.requirements.intersection(worker.capabilities)
                    )
                    total_caps = len(decision.skill.requirements)
                    score = decision.score if worker == decision.worker else 0
                    print(
                        f"{name}: eligible; provider: {worker.provider}; "
                        f"capabilities: {matched_caps}/{total_caps}; score: {score}"
                    )

            if decision.worker is None:
                print("\nSelected: NONE")
                print(f"Reason: {decision.reason}")
                print(f"Error: {decision.error_code or 'ROUTING_FAILED'}")
                return 1

            print(f"\nSelected: {decision.worker.name} -> {decision.worker.provider}")
            print(f"Reason: {decision.reason}")
            return 0

        if args.sk_command == "run":
            req = get_req(args.input, caller=args.caller, depth=args.depth)
            req = ExecutionRequest(
                skill=args.name,
                input=req.input,
                caller=req.caller,
                depth=req.depth,
            )
            res = asyncio.run(comps["dispatcher"].dispatch(req))
            print(f"Execution Status: {res.status}")
            if res.error:
                print(
                    "Execution Error: "
                    + json.dumps(res.error_details, ensure_ascii=False, sort_keys=True)
                    if res.error_details
                    else f"Execution Error: {res.error}"
                )
            elif res.output:
                print(f"Execution Output: {json.dumps(res.output, ensure_ascii=False)}")
            return 0 if res.status == "SUCCESS" else 1

    # Provider / Worker Actions
    elif args.command == "provider" and args.prov_command == "list":
        for name in comps["provider_registry"]._providers: print(name)
        return 0

    elif args.command == "worker" and args.wrk_command == "list":
        for name in comps["workers"]: print(name)
        return 0

if __name__ == "__main__":
    sys.exit(main())
