"""Unit tests for quay_vuln_report.report."""

from __future__ import annotations

import unittest

from quay_vuln_report.report import ImageRef, SecurityReportError, build_report, extract_vulnerabilities


def _feature(name, version, added_by, vulns):
    return {"Name": name, "Version": version, "AddedBy": added_by, "Vulnerabilities": vulns}


def _vuln(name, severity, fixed_by=None, link="", description=""):
    v = {"Name": name, "Severity": severity, "Link": link, "Description": description}
    if fixed_by is not None:
        v["FixedBy"] = fixed_by
    return v


IMAGE = ImageRef(
    namespace="openshift-examples",
    repo_path="sample-operator",
    tag="latest",
    digest="sha256:" + "a" * 64,
)


class ExtractVulnerabilitiesTests(unittest.TestCase):
    def test_normal_multi_layer_response(self):
        raw = {
            "status": "scanned",
            "data": {
                "Layer": {
                    "Features": [
                        _feature(
                            "openssl",
                            "1.1.1",
                            "sha256:layer1",
                            [_vuln("CVE-2024-0001", "High", fixed_by="1.1.2")],
                        ),
                        _feature(
                            "zlib",
                            "1.2.11",
                            "sha256:layer2",
                            [_vuln("CVE-2024-0002", "Low")],
                        ),
                    ]
                }
            },
        }
        records = extract_vulnerabilities(raw)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["cve"], "CVE-2024-0001")
        self.assertEqual(records[0]["package"], "openssl")
        self.assertEqual(records[0]["installed_version"], "1.1.1")
        self.assertEqual(records[0]["fixed_in_version"], "1.1.2")
        self.assertTrue(records[0]["fixable"])
        self.assertEqual(records[0]["layer_introduced_in"], "sha256:layer1")
        self.assertFalse(records[1]["fixable"])
        self.assertIsNone(records[1]["fixed_in_version"])

    def test_no_vulnerabilities(self):
        raw = {"status": "scanned", "data": {"Layer": {"Features": []}}}
        self.assertEqual(extract_vulnerabilities(raw), [])

    def test_missing_features_key_treated_as_empty(self):
        raw = {"status": "scanned", "data": {"Layer": {}}}
        self.assertEqual(extract_vulnerabilities(raw), [])

    def test_feature_with_no_vulnerabilities_key(self):
        raw = {
            "status": "scanned",
            "data": {"Layer": {"Features": [{"Name": "pkg", "Version": "1.0", "AddedBy": "l1"}]}},
        }
        self.assertEqual(extract_vulnerabilities(raw), [])

    def test_non_scanned_status_raises(self):
        raw = {"status": "queued", "data": {}}
        with self.assertRaises(SecurityReportError):
            extract_vulnerabilities(raw)

    def test_missing_data_layer_raises(self):
        with self.assertRaises(SecurityReportError):
            extract_vulnerabilities({"status": "scanned", "data": {}})
        with self.assertRaises(SecurityReportError):
            extract_vulnerabilities({"status": "scanned"})

    def test_status_none_is_treated_as_scanned(self):
        raw = {"data": {"Layer": {"Features": []}}}
        self.assertEqual(extract_vulnerabilities(raw), [])


class BuildReportTests(unittest.TestCase):
    def _raw(self, vulns_by_feature):
        features = [
            _feature(name, "1.0", f"sha256:{name}", vulns) for name, vulns in vulns_by_feature.items()
        ]
        return {"status": "scanned", "data": {"Layer": {"Features": features}}}

    def test_default_excludes_non_fixable(self):
        raw = self._raw(
            {
                "pkg-a": [_vuln("CVE-1", "High", fixed_by="2.0")],
                "pkg-b": [_vuln("CVE-2", "Low")],
            }
        )
        report = build_report(raw, IMAGE)
        self.assertEqual(report["vulnerability_count"], 1)
        self.assertEqual(report["vulnerabilities"][0]["cve"], "CVE-1")
        self.assertTrue(report["fixable_only"])

    def test_no_fixable_vulnerabilities_yields_empty_list(self):
        raw = self._raw({"pkg-a": [_vuln("CVE-1", "Low")]})
        report = build_report(raw, IMAGE)
        self.assertEqual(report["vulnerability_count"], 0)
        self.assertEqual(report["vulnerabilities"], [])

    def test_include_non_fixable_keeps_everything(self):
        raw = self._raw(
            {
                "pkg-a": [_vuln("CVE-1", "High", fixed_by="2.0")],
                "pkg-b": [_vuln("CVE-2", "Low")],
            }
        )
        report = build_report(raw, IMAGE, include_non_fixable=True)
        self.assertEqual(report["vulnerability_count"], 2)
        self.assertFalse(report["fixable_only"])

    def test_no_vulnerabilities_at_all(self):
        raw = self._raw({})
        report = build_report(raw, IMAGE)
        self.assertEqual(report["vulnerability_count"], 0)
        self.assertEqual(report["vulnerabilities"], [])

    def test_severity_sort_worst_first_and_unknown_last(self):
        raw = self._raw(
            {
                "pkg-a": [_vuln("CVE-LOW", "Low", fixed_by="1")],
                "pkg-b": [_vuln("CVE-CRIT", "Critical", fixed_by="1")],
                "pkg-c": [_vuln("CVE-WEIRD", "Weird", fixed_by="1")],
                "pkg-d": [_vuln("CVE-HIGH", "High", fixed_by="1")],
            }
        )
        report = build_report(raw, IMAGE, sort_by="severity")
        cves = [v["cve"] for v in report["vulnerabilities"]]
        self.assertEqual(cves, ["CVE-CRIT", "CVE-HIGH", "CVE-LOW", "CVE-WEIRD"])

    def test_sort_by_cve(self):
        raw = self._raw(
            {
                "pkg-a": [_vuln("CVE-2024-0002", "Low", fixed_by="1")],
                "pkg-b": [_vuln("CVE-2024-0001", "Low", fixed_by="1")],
            }
        )
        report = build_report(raw, IMAGE, sort_by="cve")
        cves = [v["cve"] for v in report["vulnerabilities"]]
        self.assertEqual(cves, ["CVE-2024-0001", "CVE-2024-0002"])

    def test_sort_by_package(self):
        raw = self._raw(
            {
                "zzz": [_vuln("CVE-1", "Low", fixed_by="1")],
                "aaa": [_vuln("CVE-2", "Low", fixed_by="1")],
            }
        )
        report = build_report(raw, IMAGE, sort_by="package")
        packages = [v["package"] for v in report["vulnerabilities"]]
        self.assertEqual(packages, ["aaa", "zzz"])

    def test_sort_by_layer(self):
        raw = {
            "status": "scanned",
            "data": {
                "Layer": {
                    "Features": [
                        _feature("a", "1.0", "sha256:zzz", [_vuln("CVE-1", "Low", fixed_by="1")]),
                        _feature("b", "1.0", "sha256:aaa", [_vuln("CVE-2", "Low", fixed_by="1")]),
                    ]
                }
            },
        }
        report = build_report(raw, IMAGE, sort_by="layer")
        layers = [v["layer_introduced_in"] for v in report["vulnerabilities"]]
        self.assertEqual(layers, ["sha256:aaa", "sha256:zzz"])

    def test_unknown_sort_by_raises(self):
        raw = self._raw({"pkg-a": [_vuln("CVE-1", "Low", fixed_by="1")]})
        with self.assertRaises(ValueError):
            build_report(raw, IMAGE, sort_by="bogus")

    def test_report_shape_includes_image_and_metadata(self):
        raw = self._raw({})
        report = build_report(raw, IMAGE)
        self.assertEqual(report["image"]["namespace"], "openshift-examples")
        self.assertEqual(report["image"]["repository"], "sample-operator")
        self.assertEqual(report["image"]["full_repository"], "openshift-examples/sample-operator")
        self.assertIn("generated_at", report)
        self.assertEqual(report["sorted_by"], "severity")


if __name__ == "__main__":
    unittest.main()
