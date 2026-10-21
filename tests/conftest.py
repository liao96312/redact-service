"""pytest 全局配置。

- 统一把项目根加入 sys.path；
- 测试默认走同进程处理（不走工作进程池），并把运行时数据放到 tests/.runtime；
- 禁用 pytest 缓存写入（受限 ACL 环境下 .pytest_cache 会报 WinError 5）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("REDACT_ISOLATE_PROCESS", "0")
os.environ.setdefault("REDACT_DATA_HOME", os.path.join(ROOT, "tests", ".runtime"))

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 需要模型/OCR 的慢测试")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_pool():
    yield
    try:
        from handlers.dispatch import shutdown_pool

        shutdown_pool()
    except Exception:  # noqa: BLE001
        pass
