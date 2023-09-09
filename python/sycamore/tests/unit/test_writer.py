from sycamore import DocSet, Context
from sycamore.execution import Node
from sycamore.execution.writes import OpenSearchWriter


class TestDocSetWriter:
    def test_opensearch(self, mocker):
        context = mocker.Mock(spec=Context)
        docset = DocSet(context, Node([mocker.Mock()]))
        execute = mocker.patch.object(OpenSearchWriter, "execute")
        docset.write.opensearch(os_client_args={}, index_name="index")
        execute.assert_called_once()
