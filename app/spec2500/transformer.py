from lxml import etree
from typing import Dict, Set, List
from collections import deque

class Spec2500Transformer:
    def __init__(self, xml_content: bytes):
        self.tree = etree.fromstring(xml_content)

    def calculate_excluded_cascade(self, excluded_pns: Set[str], pn_children_map: Dict[str, List[str]]) -> Set[str]:
        """Cascades exclusions down the hierarchy to ensure all descendant subparts are pruned."""
        all_excluded = set(excluded_pns)
        queue = deque(list(excluded_pns))
        while queue:
            curr = queue.popleft()
            for child in pn_children_map.get(curr, []):
                if child not in all_excluded:
                    all_excluded.add(child)
                    queue.append(child)
        return all_excluded

    def transform(
        self,
        pn_mapping: Dict[str, str],        # original_pn -> mapped_pn or '__EXCLUDE__'
        fp_mapping: Dict[str, str],        # original_fp -> mapped_fp
        doc_mapping: Dict[str, str],       # original_doc -> mapped_doc
        pn_children_map: Dict[str, List[str]]
    ) -> bytes:
        excluded_pns = {pn for pn, target in pn_mapping.items() if target == "__EXCLUDE__"}
        total_excluded_pns = self.calculate_excluded_cascade(excluded_pns, pn_children_map)

        # 1. Prune PartDetails for excluded PNs
        for part_elem in self.tree.xpath("//PartDetails"):
            pn_node = part_elem.xpath("./SpecificUnitInformation/PartNumber")
            if pn_node and pn_node[0].text and pn_node[0].text.strip() in total_excluded_pns:
                parent = part_elem.getparent()
                if parent is not None:
                    parent.remove(part_elem)

        # 2. Prune PartInformation entries inside SB/AD compliance lists
        for pi_elem in self.tree.xpath("//PartInformation"):
            pn_node = pi_elem.xpath(".//PartNumber")
            if pn_node and pn_node[0].text and pn_node[0].text.strip() in total_excluded_pns:
                parent = pi_elem.getparent()
                if parent is not None:
                    parent.remove(pi_elem)

        # 3. Apply Part Number Mappings across all XML tags
        valid_pn_maps = {k: v for k, v in pn_mapping.items() if v and v != "__EXCLUDE__"}
        for elem in self.tree.xpath("//PartNumber"):
            if elem.text and elem.text.strip() in valid_pn_maps:
                elem.text = valid_pn_maps[elem.text.strip()]

        # 4. Apply Functional Position Mappings
        for elem in self.tree.xpath("//PositionValue"):
            if elem.text and elem.text.strip() in fp_mapping and fp_mapping[elem.text.strip()]:
                elem.text = fp_mapping[elem.text.strip()]

        # 5. Apply Document Mappings
        for elem in self.tree.xpath("//DocumentNumber | //AD_Number"):
            if elem.text and elem.text.strip() in doc_mapping and doc_mapping[elem.text.strip()]:
                elem.text = doc_mapping[elem.text.strip()]

        return etree.tostring(self.tree, pretty_print=True, xml_declaration=True, encoding="UTF-8")