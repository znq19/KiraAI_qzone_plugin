#!/usr/bin/env python3
"""QQ空间插件自检入口。

用法（在插件根目录下）：
    python3 tests/run_tests.py            # 跑全部自检
    python3 tests/run_tests.py -v         # 显示每个用例
    python3 tests/run_tests.py dedupe     # 只跑名字含 dedupe 的用例

设计：所有网络（图片下载 / VLM / OneBot）都由桩件替代，**不会发真实请求**，
所以在任何机器上都能离线跑。
"""
import pathlib
import sys
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

MODULES = [
    'test_parser_dirty_data',
    'test_parsing_helpers',
    'test_config_consistency',
    'test_utils_fetch',
    'test_manifest_hook',
    'test_manifest_ondemand',
    'test_cookie_selfheal',
    'test_expired_image',
    'test_image_dedupe',
    'test_tool_flows',
    'test_publish_paths',
    'test_lifecycle',
    'test_review_fixes',
    'test_silent_guard',
]


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    verbose = '-v' in argv
    argv = [a for a in argv if a != '-v']
    keyword = argv[0] if argv else None

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in MODULES:
        module = __import__(name)
        if keyword:
            for case_name in dir(module):
                obj = getattr(module, case_name)
                if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
                    suite.addTests(loader.loadTestsFromTestCase(obj))
        else:
            suite.addTests(loader.loadTestsFromName(name))
    if keyword:
        filtered = unittest.TestSuite()
        for case in suite:
            if keyword.lower() in case.id().lower():
                filtered.addTest(case)
        suite = filtered

    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
