import logging
import pprint
import sys
from typing import Callable, Optional, Any, Iterable

from sycamore import Context
from sycamore.data import Document
from sycamore.plan_nodes import Node
from sycamore.transforms.embed import Embedder
from sycamore.transforms.extract_entity import EntityExtractor
from sycamore.transforms.partition import Partitioner
from sycamore.transforms.summarize import Summarizer
from sycamore.transforms.extract_table import TableExtractor
from sycamore.transforms.merge_elements import ElementMerger
from sycamore.writer import DocSetWriter

logger = logging.getLogger(__name__)


class DocSet:
    """
    A DocSet, short for “documentation set,” is a distributed collection of documents bundled together for processing.
    Sycamore provides a variety of transformations on DocSets to help customers handle unstructured data easily.
    """

    def __init__(self, context: Context, plan: Node):
        self.context = context
        self.plan = plan

    def lineage(self) -> Node:
        return self.plan

    def explain(self) -> None:
        # TODO, print out nice format DAG
        pass

    def show(
        self,
        limit: int = 20,
        show_elements: bool = True,
        num_elements: int = -1,  # -1 shows all elements
        show_binary: bool = False,
        show_embedding: bool = False,
        truncate_content: bool = True,
        truncate_length: int = 100,
        stream=sys.stdout,
    ) -> None:
        """
        Prints the content of the docset in a human-readable format. It is useful for debugging and
        inspecting the contents of objects during development.

        Args:
            limit: The maximum number of items to display.
            show_elements: Whether to display individual elements or not.
            num_elements: The number of elements to display. Use -1 to show all elements.
            show_binary: Whether to display binary data or not.
            show_embedding: Whether to display embedding information or not.
            truncate_content: Whether to truncate long content when displaying.
            truncate_length: The maximum length of content to display when truncating.
            stream: The output stream where the information will be displayed.

        Example:
             .. code-block:: python

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .show()
        """
        from sycamore import Execution

        execution = Execution(self.context, self.plan)
        dataset = execution.execute(self.plan)
        documents = [Document(row) for row in dataset.take(limit)]
        for document in documents:
            if not show_elements:
                num_elems = len(document.elements)
                document.data["elements"]["array"] = f"<{num_elems} elements>"
            else:
                if document.elements is not None and 0 <= num_elements < len(document.elements):
                    document.elements = document.elements[:num_elements]

            if not show_binary and document.binary_representation is not None:
                binary_length = len(document.binary_representation)
                document.binary_representation = f"<{binary_length} bytes>".encode("utf-8")

            if truncate_content and document.text_representation is not None:
                amount_truncated = len(document.text_representation) - truncate_length
                if amount_truncated > 0:
                    document.text_representation = (
                        document.text_representation[:truncate_length] + f" <{amount_truncated} chars>"
                    )

            if not show_embedding and document.embedding is not None:
                embedding_length = len(document.embedding)
                document.data["embedding"] = f"<{embedding_length} floats>"

            pprint.pp(document, stream=stream)

    def count(self) -> int:
        """
        Counts the number of documents in the resulting dataset.
        It is a convenient way to determine the size of the dataset generated by the plan.

        Returns:
            The number of documents in the docset.

        Example:
             .. code-block:: python

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .count()
        """
        from sycamore import Execution

        execution = Execution(self.context, self.plan)
        dataset = execution.execute(self.plan)
        return dataset.count()

    def take(self, limit: int = 20) -> list[Document]:
        """
        Returns up to ``limit`` documents from the dataset.

        Args:
            limit: The maximum number of Documents to return.

        Returns:
            A list of up to ``limit`` Documents from the Docset.

        Example:
             .. code-block:: python

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .take()

        """
        from sycamore import Execution

        execution = Execution(self.context, self.plan)
        dataset = execution.execute(self.plan)
        return [Document(row) for row in dataset.take(limit)]

    def limit(self, limit: int = 20) -> "DocSet":
        """
        Applies the Limit transforms on the Docset.

        Args:
            limit: The maximum number of documents to include in the resulting Docset.

        Example:
             .. code-block:: python

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .explode()
                    .limit()

        """
        from sycamore.transforms import Limit

        return DocSet(self.context, Limit(self.plan, limit))

    def partition(
        self, partitioner: Partitioner, table_extractor: Optional[TableExtractor] = None, **kwargs
    ) -> "DocSet":
        """
        Applies the Partition transform on the Docset.

        Example:
            .. code-block:: python

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
        """
        from sycamore.transforms import Partition

        plan = Partition(self.plan, partitioner=partitioner, table_extractor=table_extractor, **kwargs)
        return DocSet(self.context, plan)

    def spread_properties(self, props: list[str], **resource_args) -> "DocSet":
        """
        Copies listed properties from parent document to child elements.

        Example:
            .. code-block:: python

               pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .spread_properties(["title"])
                    .explode()
        """
        from sycamore.transforms import SpreadProperties

        plan = SpreadProperties(self.plan, props, **resource_args)
        return DocSet(self.context, plan)

    def explode(self, **resource_args) -> "DocSet":
        """
        Applies the Explode transform on the Docset.

        Example:
            .. code-block:: python

                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .explode()
        """
        from sycamore.transforms.explode import Explode

        explode = Explode(self.plan, **resource_args)
        return DocSet(self.context, explode)

    def embed(self, embedder: Embedder, **kwargs) -> "DocSet":
        """
        Applies the Embed transform on the Docset.

        Args:
            embedder: An instance of an Embedder class that defines the embedding method to be applied.


        Example:
            .. code-block:: python

                model_name="sentence-transformers/all-MiniLM-L6-v2"
                embedder = SentenceTransformerEmbedder(batch_size=100, model_name=model_name)

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .explode()
                    .embed(embedder=embedder)
        """
        from sycamore.transforms import Embed

        embeddings = Embed(self.plan, embedder=embedder, **kwargs)
        return DocSet(self.context, embeddings)

    def extract_entity(self, entity_extractor: EntityExtractor, **kwargs) -> "DocSet":
        """
        Applies the ExtractEntity transform on the Docset.

        Args:
            entity_extractor: An instance of an EntityExtractor class that defines the entity extraction method to be
                applied.

        Example:
             .. code-block:: python

                 title_context_template = "template"

                 openai_llm = OpenAI(OpenAIModels.GPT_3_5_TURBO.value)
                 entity_extractor = OpenAIEntityExtractor("title",
                                        llm=openai_llm,
                                        prompt_template=title_context_template)

                 context = sycamore.init()
                 pdf_docset = context.read.binary(paths, binary_format="pdf")
                     .partition(partitioner=UnstructuredPdfPartitioner())
                     .extract_entity(entity_extractor=entity_extractor)

        """
        from sycamore.transforms import ExtractEntity

        entities = ExtractEntity(self.plan, entity_extractor=entity_extractor, **kwargs)
        return DocSet(self.context, entities)

    def summarize(self, summarizer: Summarizer, **kwargs) -> "DocSet":
        """
        Applies the Summarize transform on the Docset.

        Example:
            .. code-block:: python

                llm_model = OpenAILanguageModel("gpt-3.5-turbo")
                element_operator = my_element_selector  # A custom element selection function
                summarizer = LLMElementTextSummarizer(llm_model, element_operator)

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .summarize(summarizer=summarizer)
        """
        from sycamore.transforms import Summarize

        summaries = Summarize(self.plan, summarizer=summarizer, **kwargs)
        return DocSet(self.context, summaries)

    def merge(self, merger: ElementMerger, **kwargs) -> "DocSet":
        """
        Applies merge operation on each list of elements of the Docset

        Example:
            .. code-block:: python
                from transformers import AutoTokenizer
                tk = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
                merger = GreedyElementMerger(tk, 512)

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .merge(merger=merger)
        """
        from sycamore.transforms import Merge

        merged = Merge(self.plan, merger=merger, **kwargs)
        return DocSet(self.context, merged)

    def map(self, f: Callable[[Document], Document], **resource_args) -> "DocSet":
        """
        Applies the Map transformation on the Docset.

        Args:
            f: The function to apply to each document.

        """
        from sycamore.transforms import Map

        mapping = Map(self.plan, f=f, **resource_args)
        return DocSet(self.context, mapping)

    def flat_map(self, f: Callable[[Document], list[Document]], **resource_args) -> "DocSet":
        """
        Applies the FlatMap transformation on the Docset.

        Args:
            f: The function to apply to each document.

        Example:
             .. code-block:: python

                def custom_flat_mapping_function(document: Document) -> list[Document]:
                    # Custom logic to transform the document and return a list of documents
                    return [transformed_document_1, transformed_document_2]

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                .partition(partitioner=UnstructuredPdfPartitioner())
                .flat_map(custom_flat_mapping_function)

        """
        from sycamore.transforms import FlatMap

        flat_map = FlatMap(self.plan, f=f, **resource_args)
        return DocSet(self.context, flat_map)

    def filter(self, f: Callable[[Document], bool], **resource_args) -> "DocSet":
        """
        Applies the Filter transform on the Docset.

        Args:
            f: A callable function that takes a Document object and returns a boolean indicating whether the document
                should be included in the filtered Docset.

        Example:
             .. code-block:: python

                def custom_filter(doc: Document) -> bool:
                    # Define your custom filtering logic here.
                    return doc.some_property == some_value

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .filter(custom_filter)

        """
        from sycamore.transforms import Filter

        filtered = Filter(self.plan, f=f, **resource_args)
        return DocSet(self.context, filtered)

    def map_batch(
        self,
        f: Callable[[list[Document]], list[Document]],
        f_args: Optional[Iterable[Any]] = None,
        f_kwargs: Optional[dict[str, Any]] = None,
        f_constructor_args: Optional[Iterable[Any]] = None,
        f_constructor_kwargs: Optional[dict[str, Any]] = None,
        **resource_args,
    ) -> "DocSet":
        from sycamore.transforms import MapBatch

        map_batch = MapBatch(
            self.plan,
            f=f,
            f_args=f_args,
            f_kwargs=f_kwargs,
            f_constructor_args=f_constructor_args,
            f_constructor_kwargs=f_constructor_kwargs,
            **resource_args,
        )
        return DocSet(self.context, map_batch)

    @property
    def write(self) -> DocSetWriter:
        return DocSetWriter(self.context, self.plan)
