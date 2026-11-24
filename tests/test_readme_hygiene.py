"""README 亮点区卫生守卫：不允许无限膨胀，也不允许堆版本号。

背景：亮点区曾经从 5 条涨到 12 条，每条都带 v1.4.x 版本号，越写越长。
版本变更属于 `更新记录.txt`；README 只保留"现在有什么能力"。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

MAX_BULLETS = 8
MAX_BULLET_CHARS = 120


class TestReadmeHighlights(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / 'README.md').read_text(encoding='utf-8')
        start = cls.text.index('## ✨ 插件亮点')
        end = cls.text.index('\n## ', start + 1)
        cls.section = cls.text[start:end]
        cls.bullets = [
            line.strip() for line in cls.section.splitlines()
            if line.strip().startswith('- ')
        ]

    def test_section_exists(self):
        self.assertTrue(self.bullets, '缺少「插件亮点」条目')

    def test_bullet_count_is_bounded(self):
        self.assertLessEqual(
            len(self.bullets), MAX_BULLETS,
            f'亮点条目 {len(self.bullets)} 条，超过上限 {MAX_BULLETS}；'
            '请合并同类项，不要把更新记录搬到这里',
        )

    def test_no_version_numbers(self):
        bad = [b for b in self.bullets if re.search(r'v\d+\.\d+', b)]
        self.assertEqual(
            bad, [],
            '亮点区不允许写版本号；版本变更请写进 更新记录.txt',
        )

    def test_bullets_are_short(self):
        bad = [(len(b), b) for b in self.bullets if len(b) > MAX_BULLET_CHARS]
        self.assertEqual(
            bad, [],
            f'亮点条目过长（上限 {MAX_BULLET_CHARS} 字）：{bad}',
        )

    def test_no_version_changelog_in_header(self):
        """顶部不允许再堆 v1.4.x 更新说明（曾经堆了 3 条，越写越长）。"""
        head = self.text.split('## ', 1)[0]
        bad = [
            line.strip() for line in head.splitlines()
            if line.strip().startswith('>') and re.search(r'v\d+\.\d+[：:]', line)
        ]
        self.assertEqual(bad, [], f'README 顶部不允许版本更新说明：{bad}')


if __name__ == '__main__':
    unittest.main()
