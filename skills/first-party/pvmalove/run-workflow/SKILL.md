---
name: run-workflow
description: Execute a declarative multi-step workflow via the Harness Orchestrator. Use this when the user asks to run a workflow (e.g., feature-development, bug-fix).
---

# Workflow Orchestration

You are integrated with the **Harness Workflow Orchestrator**, a dynamic dispatcher for multi-step AI tasks. 

When the user asks you to run a workflow or execute a complex pipeline (e.g., "Запусти workflow feature-development для TASK-123"), **DO NOT** attempt to perform the individual sub-tasks (research, planning, coding, reviewing, testing) manually. Instead, delegate the entire pipeline to the Harness runtime.

## How to use Harness

1. **Check available workflows** (if you don't know the exact name):
   ```bash
   harness workflow list
   ```

2. **Preview the execution plan** (optional, to see which workers will handle which steps):
   ```bash
   harness workflow plan <workflow-name>
   ```

3. **Run the workflow**:
   Execute the workflow and pass the user's task context as JSON input.
   ```bash
   harness workflow run <workflow-name> --input '{"task": "Add OAuth support..."}'
   ```
   The engine will output a live stream of the execution, step by step:
   ```text
   [1/7] grill-with-docs      -> claude
         [OK] completed
   [2/7] to-spec              -> claude
         [OK] completed
   ...
   ```
   Relay this progress directly back to the user.

4. **Resuming Failures**:
   If a workflow fails at step 4, you don't need to restart from step 1. The output will provide an execution state ID. After fixing the underlying issue with the user, simply run:
   ```bash
   harness workflow resume <execution_id>
   ```

## Your Role
You are the **User Frontend**. Harness is the **Backend Orchestrator**. 
Your job is to parse the user's intent, trigger `workflow run`, relay the beautiful execution stream back to the user, and help them debug if a step fails!
