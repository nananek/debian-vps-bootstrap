"""テスト共通: リポジトリルートを import パスへ通し bootstrap を読み込む。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bootstrap  # noqa: E402  (sys.path 調整後に import する必要がある)

EXAMPLES = os.path.join(ROOT, "examples")

# 各テストモジュールが `from _helpers import bootstrap` で参照する再エクスポート。
__all__ = ["bootstrap", "EXAMPLES", "ROOT"]
