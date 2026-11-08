"""元数据一致性：插件注册版本号必须与 metadata.yaml 对齐。

版本号有两处独立维护（main.py 的 @register 与 metadata.yaml），历史上因为
两边不同步被审查指出版本不一致，用一个用例把漂移钉住
（PR #289 审查：version-mismatch）。
"""

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VersionConsistencyTests(unittest.TestCase):
    def test_register_version_matches_metadata(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        match = re.search(r"@register\((.*?)\)\s*\nclass ", source, re.S)
        self.assertIsNotNone(match, "main.py 里找不到 @register(...) 装饰器")
        versions = re.findall(r'"(\d+\.\d+\.\d+)"', match.group(1))
        self.assertTrue(versions, "@register 里找不到版本号字面量")
        register_version = versions[0]

        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        meta_match = re.search(r"^version:\s*v?([0-9.]+)\s*$", metadata, re.M)
        self.assertIsNotNone(meta_match, "metadata.yaml 里找不到 version 字段")
        self.assertEqual(
            register_version,
            meta_match.group(1),
            "main.py 的 @register 版本号与 metadata.yaml 不一致",
        )

    def test_no_stale_version_in_code(self):
        # 代码/配置里不该残留 2.7.x 及更早的版本字面量（CHANGELOG 的历史条目除外）
        stale = []
        for path in ROOT.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            if re.search(r"\"2\.[0-6]\.\d+\"", path.read_text(encoding="utf-8")):
                stale.append(str(path.relative_to(ROOT)))
        self.assertEqual(stale, [], f"以下文件仍硬编码旧版本号: {stale}")


if __name__ == "__main__":
    unittest.main()
