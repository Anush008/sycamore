from sycamore.execution.scans import BinaryScan
from sycamore.tests.config import TEST_DIR


class TestBinaryScan:
    def test_unstructured_partition(self):
        paths = str(TEST_DIR / "resources/data/pdfs/")
        scan = BinaryScan(paths, binary_format="pdf")
        ds = scan.execute()
        assert (ds.schema().names ==
                ['doc_id', 'type', 'searchable_text', 'content', 'elements',
                 'embedding', 'parent_id', 'properties'])
