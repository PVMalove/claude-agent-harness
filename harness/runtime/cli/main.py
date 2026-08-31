import argparse
import asyncio
import json
import sys
from pathlib import Path

from ..infrastructure.config import load_config
from ..domain.skill import Skill
from ..domain.worker import Worker
from ..domain.policy import ExecutionPolicy
from ..domain.execution import ExecutionRequest
from ..application.registry import ProviderRegistry
from ..application.resolver import CapabilityResolver
from ..application.dispatcher import (
    SkillRegistry,
    PolicyEngine,
    HealthRegistry,
    Scheduler,
    Planner,
    Executor,
    Dispatcher
)
from ..infrastructure.providers.cli_provider import CLIProvider
from ..infrastructure.providers.mcp_provider import MCPProvider

def main():
    parser = argparse.ArgumentParser(prog="harness.runtime.cli")
    parser.add_argument("--repo", default=".", help="Repository path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    explain_parser = subparsers.add_parser("explain")
    explain_parser.add_argument("skill")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("skill")

    providers_parser = subparsers.add_parser("providers")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("skill")
    run_parser.add_argument("--input", default="{}", help="JSON input")

    args = parser.parse_args()

    repo_path = Path(args.repo)
    config_path = repo_path / ".harness" / "orchestration.toml"
    
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"Configuration file not found: {config_path}")
        return 1

    # Initialize domain models from config
    skills = {}
    for name, data in config.get("skills", {}).items():
        reqs = set(data.get("requires", []))
        pref = data.get("execution", {}).get("preferred", [])
        policy = ExecutionPolicy(preferred=pref)
        skills[name] = Skill(name=name, requirements=reqs, execution_policy=policy)
        
    workers = {}
    for name, data in config.get("workers", {}).items():
        prov = data.get("provider")
        caps = set(data.get("capabilities", []))
        workers[name] = Worker(name=name, provider=prov, capabilities=caps)

    # Initialize infrastructure
    provider_registry = ProviderRegistry()
    for prov_name, prov_data in config.get("providers", {}).items():
        ptype = prov_data.get("type")
        command = prov_data.get("command")
        prov_args = prov_data.get("args", [])
        
        if ptype == "mcp":
            provider_registry.register(prov_name, MCPProvider(command, prov_args))
        elif ptype == "llm" and command == "claude":
            from ..infrastructure.providers.cli_provider import ClaudeProvider
            provider_registry.register(prov_name, ClaudeProvider(command, prov_args))
        elif ptype == "cli" and command == "agy":
            from ..infrastructure.providers.cli_provider import AntigravityProvider
            provider_registry.register(prov_name, AntigravityProvider(command, prov_args))
        elif ptype == "cli" and command == "codex":
            from ..infrastructure.providers.cli_provider import CodexProvider
            provider_registry.register(prov_name, CodexProvider(command, prov_args))
        else:
            provider_registry.register(prov_name, CLIProvider(command, prov_args))
    
    # Initialize application layer
    skill_registry = SkillRegistry(skills)
    resolver = CapabilityResolver(workers)
    policy_engine = PolicyEngine()
    health_registry = HealthRegistry()
    scheduler = Scheduler()
    planner = Planner()
    executor = Executor(provider_registry)
    
    dispatcher = Dispatcher(
        skills=skill_registry,
        policy=policy_engine,
        resolver=resolver,
        health=health_registry,
        scheduler=scheduler,
        planner=planner,
        executor=executor
    )

    if args.command == "providers":
        print("Registered Providers:")
        for name, provider in provider_registry._providers.items():
            print(f"  - {name}: {provider.__class__.__name__}")
        return 0
        
    try:
        input_data = json.loads(args.input) if getattr(args, 'input', None) else {}
    except json.JSONDecodeError:
        print("Invalid JSON input")
        return 1

    request = ExecutionRequest(
        skill=args.skill,
        input=input_data,
        caller="user",
        session_id="session-1",
        project_id="project-1"
    )

    if args.command == "explain":
        try:
            skill = skill_registry.resolve(args.skill)
            print(f"Skill: {skill.name}")
            print(f"Requirements:")
            for req in skill.requirements:
                print(f"  * {req}")
            candidates = resolver.resolve_candidates(skill.requirements)
            print("\nCandidates:")
            for c in candidates:
                print(f"  {c.name}")
                print(f"    * capabilities")
            
            preferred = skill.execution_policy.preferred
            if preferred:
                preferred_candidates = [c for c in candidates if c.name in preferred]
                if preferred_candidates:
                    candidates = preferred_candidates
            
            worker = scheduler.select(candidates, request)
            print(f"\nSelected Worker: {worker.name} (provider: {worker.provider})")
        except Exception as e:
            print(f"Explain failed: {e}")
            return 1
        return 0

    if args.command == "plan":
        try:
            skill = skill_registry.resolve(args.skill)
            candidates = resolver.resolve_candidates(skill.requirements)
            
            preferred = skill.execution_policy.preferred
            if preferred:
                preferred_candidates = [c for c in candidates if c.name in preferred]
                if preferred_candidates:
                    candidates = preferred_candidates
                    
            worker = scheduler.select(candidates, request)
            policy = policy_engine.evaluate(request, skill)
            plan = planner.create(request, skill, worker, policy)
            print(f"Plan for skill '{plan.skill}':")
            print(f"  Worker: {plan.worker}")
            print(f"  Provider: {plan.provider}")
            print(f"  Timeout: {plan.timeout}")
            print(f"  Capabilities: {', '.join(plan.capabilities)}")
        except Exception as e:
            print(f"Plan failed: {e}")
            return 1
        return 0

    if args.command == "run":
        try:
            result = asyncio.run(dispatcher.dispatch(request))
            print(f"Execution Status: {result.status}")
            if result.error:
                print(f"Error: {result.error}")
            if result.output:
                print(f"Output: {json.dumps(result.output)}")
            return 0 if result.status == "SUCCESS" else 1
        except Exception as e:
            print(f"Run failed: {e}")
            return 1

if __name__ == "__main__":
    sys.exit(main())
