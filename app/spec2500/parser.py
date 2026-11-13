from lxml import etree
from typing import Dict, List, Set, Any
from collections import defaultdict

class Spec2500Parser:
    def __init__(self, xml_content: bytes):
        self.tree = etree.fromstring(xml_content)

    def extract_root_pn(self) -> str:
        # Check Engine PartNumber in AssetInformation
        for elem in self.tree.xpath("//AssetInformation/Engine/PartNumber"):
            if elem.text and elem.text.strip():
                return elem.text.strip()
        # Fallback to AssetStatus
        for elem in self.tree.xpath("//ATA_AssetStatus/Engine/PartNumber"):
            if elem.text and elem.text.strip():
                return elem.text.strip()
        # Fallback to first PartDetails
        for elem in self.tree.xpath("//PartDetails[1]/SpecificUnitInformation/PartNumber"):
            if elem.text and elem.text.strip():
                return elem.text.strip()
        return ""

    def extract_assembly_and_elements(self) -> Dict[str, Any]:
        """Extracts all unique sub-PNs, hierarchy graph, Functional Positions, and Documents."""
        sub_pns: Set[str] = set()
        fp_codes: Set[str] = set()
        doc_numbers: Set[str] = set()
        pn_parent_map: Dict[str, str] = {} # child_pn -> parent_pn
        pn_children_map: Dict[str, Set[str]] = defaultdict(set) # parent_pn -> set(child_pns)
        pn_descriptions: Dict[str, str] = {}
        # Read descriptions belonging to the PN's own record, never from its NHA.
        for pn_elem in self.tree.xpath('//PartNumber'):
            if not pn_elem.text or not pn_elem.text.strip():
                continue
            container = pn_elem.getparent()
            descriptions = container.xpath('./PartDescription | ./Description | ./Nomenclature | ./PartName')
            if container.tag == 'SpecificUnitInformation':
                descriptions += container.getparent().xpath('./PartDescription | ./Description | ./Nomenclature | ./PartName')
            for description in descriptions:
                value = ' '.join(''.join(description.itertext()).split())
                if value:
                    pn_descriptions.setdefault(pn_elem.text.strip(), value)
                    break

        # 1. Parse PartDetails
        for part_elem in self.tree.xpath("//PartDetails"):
            pn_node = part_elem.xpath("./SpecificUnitInformation/PartNumber")
            if not pn_node or not pn_node[0].text:
                continue
            pn = pn_node[0].text.strip()
            sub_pns.add(pn)

            # Functional Position
            for fp in part_elem.xpath(".//PositionInformation/FunctionalPositionInformationList/FunctionalPositionInformation/PositionValue"):
                if fp.text and fp.text.strip():
                    fp_codes.add(fp.text.strip())

            # Documents inside PartDetails
            for doc in part_elem.xpath(".//OtherDocuments/DocumentNumber"):
                if doc.text and doc.text.strip():
                    doc_numbers.add(doc.text.strip())
            for doc in part_elem.xpath(".//TrackedEvent/OperatorTaskInfo/DocumentNumber"):
                if doc.text and doc.text.strip() and doc.text.strip() != "NOT DEFINED":
                    doc_numbers.add(doc.text.strip())

            # Next higher assembly hierarchy
            nha_pn = part_elem.xpath("./NextHigherAssemblyInformation/NHA_PartInfo/PartNumber")
            if nha_pn and nha_pn[0].text:
                parent_pn = nha_pn[0].text.strip()
                pn_parent_map[pn] = parent_pn
                pn_children_map[parent_pn].add(pn)
            else:
                eng_pn = part_elem.xpath("./NextHigherAssemblyInformation/EngineInfo/PartNumber")
                if eng_pn and eng_pn[0].text:
                    parent_pn = eng_pn[0].text.strip()
                    pn_parent_map[pn] = parent_pn
                    pn_children_map[parent_pn].add(pn)

        # 2. Parse SB/Mod/AD Documents across entire Spec2500 crate
        for doc in self.tree.xpath("//PrimaryDocumentRef/DocumentNumber"):
            if doc.text and doc.text.strip():
                doc_numbers.add(doc.text.strip())
        for doc in self.tree.xpath("//AD_Num/AD_Number"):
            if doc.text and doc.text.strip():
                doc_numbers.add(doc.text.strip())
        for doc in self.tree.xpath("//PositionValue"):
            if doc.text and doc.text.strip():
                fp_codes.add(doc.text.strip())
        for doc in self.tree.xpath("//Part_ID_Information/PartNumber"):
            if doc.text and doc.text.strip():
                sub_pns.add(doc.text.strip())

        return {
            "root_pn": self.extract_root_pn(),
            "pn_descriptions": pn_descriptions,
            "sub_pns": sorted(list(sub_pns)),
            "fp_codes": sorted(list(fp_codes)),
            "doc_numbers": sorted(list(doc_numbers)),
            "pn_parent_map": pn_parent_map,
            "pn_children_map": {k: list(v) for k, v in pn_children_map.items()}
        }
