"""Documentation covers retain real terminal content in Qt's SVG Tiny renderer."""

from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import render_preview


class PreviewCoverTests(unittest.TestCase):
    def test_cover_flattens_terminal_viewports_without_losing_content(self):
        namespace = "{http://www.w3.org/2000/svg}"
        cover = ET.fromstring(render_preview.render(surface="cover"))
        self.assertEqual(cover.tag, namespace + "svg")
        self.assertEqual(len(list(cover.iter(namespace + "svg"))), 1,
                         "Qt SVG Tiny silently drops nested SVG terminal panels")
        groups = cover.findall(namespace + "g")
        self.assertEqual(len(groups), 2)
        for group, surface, width, transform in [
            (groups[0], "capacity", 80, "translate(32 196)"),
            (groups[1], "port-form", 52, "translate(872 196)"),
        ]:
            with self.subTest(surface=surface):
                original = ET.fromstring(render_preview.render(width, 28, surface=surface))
                self.assertEqual(group.attrib, {"transform": transform})
                self.assertEqual(len(group), len(original))
                for actual, expected in zip(group, original):
                    expected_attributes = dict(expected.attrib)
                    if expected.get("width") == "100%":
                        expected_attributes.update(width=str(width * 10), height="616")
                    self.assertEqual(actual.tag, expected.tag)
                    self.assertEqual(actual.attrib, expected_attributes)
                    self.assertEqual(actual.text, expected.text)
                self.assertGreater(len(group.findall(namespace + "text")), 100)


if __name__ == "__main__":
    unittest.main()
