"""harness/health: реестр проверок `harness health` только на стандартной библиотеке.

Форма результатов описана в `model.py`, а подключение проверок — в `registry.py`.

`harness/bin/harness.py` уже добавляет канонический корень репозитория в `sys.path` перед
импортом этого пакета, поэтому `import harness.health` работает и без блока ниже. Этот блок
имеет значение только при побайтовом копировании пакета в `.harness/health/` целевого проекта
(аналогично тому, как ресурсы `CAPABILITIES.json` копируют `harness/repo_map/repo_map.py`) и
последующем запуске без канонического пакета `harness/` рядом в `sys.path` — консоль харнесса
(#348) выступает первым потребителем этого пути. Это повторяет псевдоним начальной загрузки
(bootstrap alias), используемый в `harness/repo_map/repo_map.py`; см. docs/adr/0001.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HARNESS_ROOT: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT: Path = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness" and "harness" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)
