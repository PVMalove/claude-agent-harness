import os
import subprocess
from mcp.server.mcpserver import MCPServer

# Создаем инстанс MCP сервера
mcp = MCPServer("Antigravity-Harness-Wrapper")

# Определяем корень проекта. По умолчанию - это родительская папка (корень claude-agent-harness)
DEFAULT_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = os.environ.get("HARNESS_PROJECT_ROOT", DEFAULT_PROJECT_ROOT)

def invoke_agy_qa_gate() -> str:
    """Вспомогательная функция для вызова agy."""
    cmd = [
        "agy", 
        "--prompt", "/qa-gate",
        "--dangerously-skip-permissions" # Флаг для неинтерактивного запуска, если применимо
    ]
    
    try:
        # Запускаем дочерний процесс
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True
        )
        return f"✅ Проверка qa-gate завершена.\nВывод:\n{result.stdout}"
    except subprocess.CalledProcessError as e:
        # В случае ошибки отдаем логи клиенту
        return (
            f"❌ Ошибка выполнения qa-gate (Код: {e.returncode})\n"
            f"STDOUT:\n{e.stdout}\n"
            f"STDERR:\n{e.stderr}"
        )
    except FileNotFoundError:
         return "❌ Ошибка: исполняемый файл 'agy' не найден. Убедитесь, что Antigravity CLI установлен и доступен в PATH."

@mcp.tool()
def run_qa_gate() -> str:
    """
    Запускает скилл qa-gate через Antigravity (agy). 
    Выполняет полный набор локальных проверок качества кода (линтеры, тайпчекеры, тесты),
    настроенных в .harness/project.json. 
    Используй этот инструмент перед открытием Pull Request или по запросу пользователя.
    """
    return invoke_agy_qa_gate()

if __name__ == "__main__":
    # Запуск сервера
    mcp.run()
