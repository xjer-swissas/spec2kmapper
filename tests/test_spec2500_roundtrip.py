import asyncio
import tempfile
import unittest

from fastapi import HTTPException, UploadFile

from app.routers.spec2500_routes import (
    PARSED_SESSIONS,
    MatchCandidatesRequest,
    TransformRequest,
    export_transformed_spec2500,
    get_candidates_for_root,
    upload_spec2500,
)
from app.spec2500.parser import Spec2500Parser


class AmosSnapshot:
    """Small AMOS snapshot with configuration only for the actual root."""

    def execute(self, statement, params=None):
        sql = str(statement)
        if "ranking_mode" in sql:
            rows = [("weighted", False, False, 0.25, "none")]
        elif "amos_part_master" in sql:
            rows = [("ENGINE-1", ""), ("CHILD-1", "")]
        elif 'UNION SELECT parent_pn' in sql:
            rows = [("ENGINE-1",)]
        elif params["p"] != "ENGINE-1":
            rows = []
        elif "pn_part_of_pn_conf" in sql:
            rows = [("CHILD-1",)]
        elif "fp_part_of_pn_conf" in sql:
            rows = [("FP-1",)]
        else:
            rows = [("DOC-1",)]

        class Result:
            def fetchone(self):
                return rows[0] if rows else None

            def fetchall(self):
                return rows

        return Result()


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.db = AmosSnapshot()
        self.user = {"schema_name": "test_tenant"}
        self.sessions = []

    def tearDown(self):
        for session_id in self.sessions:
            PARSED_SESSIONS.pop(session_id, None)

    def upload(self, xml):
        with tempfile.SpooledTemporaryFile() as file:
            file.write(xml)
            file.seek(0)
            result = asyncio.run(upload_spec2500(
                UploadFile(filename="test.xml", file=file),
                user=self.user, db=self.db,
            ))
        self.sessions.append(result["session_id"])
        return result

    def candidates(self, upload, root="ENGINE-1"):
        return get_candidates_for_root(
            upload["session_id"], MatchCandidatesRequest(root_pn=root),
            user=self.user, db=self.db,
        )

    def test_export_reingestion_preserves_confirmed_root_and_exact_matches(self):
        # Cover a root only in AssetInformation and one repeated in PartDetails.
        for include_root_part in (False, True):
            for root_mapping in ("CHILD-1", "__EXCLUDE__"):
                with self.subTest(root_part=include_root_part, mapping=root_mapping):
                    root_part = (
                        "<PartDetails><SpecificUnitInformation>"
                        "<PartNumber>ENGINE 1</PartNumber>"
                        "</SpecificUnitInformation></PartDetails>"
                        if include_root_part else ""
                    )
                    xml = f"""<Crate>
                      <AssetInformation><Engine><PartNumber>ENGINE 1</PartNumber></Engine></AssetInformation>
                      {root_part}
                      <PartDetails>
                        <SpecificUnitInformation><PartNumber>CHILD 1</PartNumber></SpecificUnitInformation>
                        <NextHigherAssemblyInformation><EngineInfo><PartNumber>ENGINE 1</PartNumber></EngineInfo></NextHigherAssemblyInformation>
                        <PositionValue>FP 1</PositionValue>
                        <OtherDocuments><DocumentNumber>DOC 1</DocumentNumber></OtherDocuments>
                      </PartDetails>
                    </Crate>""".encode()
                    first = self.upload(xml)
                    candidates = self.candidates(first)
                    if include_root_part:
                        self.assertNotIn("ENGINE 1", candidates["pn_matches"])
                    response = export_transformed_spec2500(TransformRequest(
                        session_id=first["session_id"],
                        pn_mapping={"ENGINE 1": root_mapping, "CHILD 1": "CHILD-1"},
                        fp_mapping={"FP 1": "FP-1"}, doc_mapping={"DOC 1": "DOC-1"},
                    ), user=self.user)
                    parsed = Spec2500Parser(response.body).extract_assembly_and_elements()
                    self.assertEqual(parsed["root_pn"], "ENGINE-1")
                    self.assertEqual(parsed["pn_parent_map"]["CHILD-1"], "ENGINE-1")
                    second = self.upload(response.body)
                    self.assertEqual(second["root_matches"][0]["candidate"], "ENGINE-1")
                    matches = self.candidates(second, second["root_matches"][0]["candidate"])
                    self.assertIsNone(matches["configuration_warning"])
                    for category in ("pn_matches", "fp_matches", "doc_matches"):
                        self.assertTrue(matches[category])
                        for ranked in matches[category].values():
                            self.assertEqual(ranked[0]["match_type"], "EXACT")
                            self.assertEqual(ranked[0]["score"], 100.0)

    def test_missing_configuration_is_reported(self):
        upload = self.upload(b"<Crate><PartDetails><SpecificUnitInformation><PartNumber>CHILD-1</PartNumber></SpecificUnitInformation></PartDetails></Crate>")
        matches = self.candidates(upload, "CHILD-1")
        self.assertEqual(matches["available_pns_pool"], [])
        self.assertIn('root "CHILD-1"', matches["configuration_warning"])

    def test_export_requires_root_confirmation(self):
        upload = self.upload(b"<Crate/>")
        with self.assertRaises(HTTPException) as error:
            export_transformed_spec2500(TransformRequest(
                session_id=upload["session_id"], pn_mapping={}, fp_mapping={}, doc_mapping={},
            ), user=self.user)
        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
