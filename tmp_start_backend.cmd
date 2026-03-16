@echo off
cd /d "d:\Work\Project Automatron\orchestrator"
start "" /b "d:\Work\Project Automatron\orchestrator\.venv\Scripts\python.exe" -m uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000 > "d:\Work\Project Automatron\orchestrator\uvicorn.stdout.log" 2> "d:\Work\Project Automatron\orchestrator\uvicorn.stderr.log"
