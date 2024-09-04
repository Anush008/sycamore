import logging
from typing import Any, Callable, Optional, Union, TYPE_CHECKING

from pyarrow.fs import FileSystem

from sycamore.context import Context, ExecMode, context_params
from sycamore.connectors.common import HostAndPort
from sycamore.connectors.file.file_writer import default_doc_to_bytes, default_filename, FileWriter, JsonWriter
from sycamore.data import Document
from sycamore.executor import Execution
from sycamore.plan_nodes import Node
from sycamore.docset import DocSet
from sycamore.utils.import_utils import requires_modules

if TYPE_CHECKING:
    from neo4j import Auth
    from neo4j.auth_management import AuthManager
    from boto3.session import Session


logger = logging.getLogger(__name__)


class DocSetWriter:
    """
    Contains interfaces for writing to external storage systems, most notably OpenSearch.

    Users should not instantiate this class directly, but instead access an instance using
    :meth:`sycamore.docset.DocSet.write`
    """

    def __init__(self, context: Context, plan: Node):
        self.context = context
        self.plan = plan

    @context_params
    def opensearch(
        self,
        *,
        os_client_args: dict,
        index_name: str,
        index_settings: dict,
        execute: bool = True,
        **kwargs,
    ) -> Optional["DocSet"]:
        """Writes the content of the DocSet into the specified OpenSearch index.

        Args:
            os_client_args: Keyword parameters that are passed to the opensearch-py OpenSearch client constructor.
                See more information at https://opensearch.org/docs/latest/clients/python-low-level/
            index_name: The name of the OpenSearch index into which to load this DocSet.
            index_settings: Settings and mappings to pass when creating a new index. Specified as a Python dict
                corresponding to the JSON paramters taken by the OpenSearch CreateIndex API:
                https://opensearch.org/docs/latest/api-reference/index-apis/create-index/
            execute: Execute the pipeline and write to opensearch on adding this operator. If false,
                will return a new docset with the write in the plan
            kwargs: Arguments to pass to the underlying execution engine

        Example:
            The following code shows how to read a pdf dataset into a ``DocSet`` and write it out to a
            local OpenSearch index called `my_index`.

            .. code-block:: python

                os_client_args = {
                    "hosts": [{"host": "localhost", "port": 9200}],
                    "http_auth": ("user", "password"),
                }

                index_settings = {
                    "body": {
                        "settings": {
                            "index.knn": True,
                        },
                        "mappings": {
                            "properties": {
                                "embedding": {
                                    "type": "knn_vector",
                                    "dimension": 384,
                                    "method": {"name": "hnsw", "engine": "faiss"},
                                },
                            },
                        },
                    },
                }

                context = sycamore.init()
                pdf_docset = context.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())

                pdf.write.opensearch(
                     os_client_args=os_client_args,
                     index_name="my_index",
                     index_settings=index_settings)
        """

        from sycamore.connectors.opensearch import (
            OpenSearchWriter,
            OpenSearchWriterClientParams,
            OpenSearchWriterTargetParams,
        )
        from typing import Any
        import copy

        # We mutate os_client_args, so mutate a copy
        os_client_args = copy.deepcopy(os_client_args)

        # Type narrowing for hosts joy
        def _convert_to_host_port_list(hostlist: Any) -> list[HostAndPort]:
            if not isinstance(hostlist, list):
                raise ValueError('OpenSearch client args "hosts" param must be a list of hosts')
            for h in hostlist:
                if (
                    not isinstance(h, dict)
                    or "host" not in h
                    or not isinstance(h["host"], str)
                    or "port" not in h
                    or not isinstance(h["port"], int)
                ):
                    raise ValueError(
                        'OpenSearch client args "hosts" objects must consist of dicts of '
                        "the form {'host': '<address>', 'port': <port num>}\n"
                        f"Found: {h}"
                    )
            return [HostAndPort(host=h["host"], port=h["port"]) for h in hostlist]

        hosts = os_client_args.get("hosts", None)
        if hosts is not None:
            os_client_args["hosts"] = _convert_to_host_port_list(hosts)
        client_params = OpenSearchWriterClientParams(**os_client_args)

        target_params: OpenSearchWriterTargetParams

        if index_settings is not None:
            idx_settings = index_settings.get("body", {}).get("settings", {})
            idx_mappings = index_settings.get("body", {}).get("mappings", {})
            target_params = OpenSearchWriterTargetParams(index_name, idx_settings, idx_mappings)
        else:
            target_params = OpenSearchWriterTargetParams(index_name, {}, {})

        os = OpenSearchWriter(
            self.plan, client_params=client_params, target_params=target_params, name="OsrchWrite", **kwargs
        )

        # We will probably want to break this at some point so that write
        # doesn't execute automatically, and instead you need to say something
        # like docset.write.opensearch().execute(), allowing sensible writes
        # to multiple locations and post-write operations.
        osds = DocSet(self.context, os)
        if execute:
            # If execute, force execution
            osds.execute()
            return None
        else:
            return osds

    @requires_modules(["weaviate", "weaviate.collections.classes.config"], extra="weaviate")
    def weaviate(
        self,
        *,
        wv_client_args: dict,
        collection_name: str,
        collection_config: Optional[dict[str, Any]] = None,
        flatten_properties: bool = False,
        execute: bool = True,
        **kwargs,
    ) -> Optional["DocSet"]:
        """Writes the content of the DocSet into the specified Weaviate collection.

        Args:
            wv_client_args: Keyword parameters that are passed to the weaviate client constructor.
                See more information at https://weaviate.io/developers/weaviate/client-libraries/python#python-client-v4-explicit-connection
            collection_name: The name of the Weaviate collection into which to load this DocSet.
            collection_config: Keyword parameters that are passed to the weaviate client's `collections.create()`
                method.If not provided, Weaviate will Auto-Schematize the incoming records, which may lead to
                inconsistencies or failures. See more information at
                https://weaviate.io/developers/weaviate/manage-data/collections#create-a-collection-and-define-properties
            flatten_properties: Whether to flatten documents into pure key-value pairs or to allow nested
                structures. Default is False (allow nested structures)
            execute: Execute the pipeline and write to weaviate on adding this operator. If False,
                will return a DocSet with this write in the plan. Default is True
            kwargs: Arguments to pass to the underlying execution engine

        Example:
            The following code shows how to read a pdf dataset into a ``DocSet`` and write it out to a
            local Weaviate collection called `DemoCollection`.

            .. code-block:: python


                collection = "DemoCollection"
                wv_client_args = {
                    "connection_params": ConnectionParams.from_params(
                        http_host="localhost",
                        http_port=8080,
                        http_secure=False,
                        grpc_host="localhost",
                        grpc_port=50051,
                        grpc_secure=False,
                    )
                }

                # Weaviate will assume empty arrays are empty arrays of text, so it
                # will throw errors when you try to make an array of non-text in a
                # field that some records have empty. => We specify them here.
                collection_config_params = {
                    "name": collection,
                    "description": "A collection to demo data-prep with sycamore",
                    "properties": [
                        Property(
                            name="properties",
                            data_type=DataType.OBJECT,
                            nested_properties=[
                                Property(
                                    name="links",
                                    data_type=DataType.OBJECT_ARRAY,
                                    nested_properties=[
                                        Property(name="text", data_type=DataType.TEXT),
                                        Property(name="url", data_type=DataType.TEXT),
                                        Property(name="start_index", data_type=DataType.NUMBER),
                                    ],
                                ),
                            ],
                        ),
                        Property(name="bbox", data_type=DataType.NUMBER_ARRAY),
                        Property(name="shingles", data_type=DataType.INT_ARRAY),
                    ],
                    "vectorizer_config": [Configure.NamedVectors.text2vec_transformers(name="embedding")],
                    "references": [ReferenceProperty(name="parent", target_collection=collection)],
                }

                model_name = "sentence-transformers/all-MiniLM-L6-v2"
                davinci_llm = OpenAI(OpenAIModels.GPT_3_5_TURBO_INSTRUCT.value)
                tokenizer = HuggingFaceTokenizer(model_name)

                ctx = sycamore.init()

                ds = ctx.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .regex_replace(COALESCE_WHITESPACE)
                    .extract_entity(entity_extractor=OpenAIEntityExtractor(
                            "title", llm=davinci_llm, prompt_template=title_template))
                    .mark_bbox_preset(tokenizer=tokenizer)
                    .merge(merger=MarkedMerger())
                    .spread_properties(["path", "title"])
                    .split_elements(tokenizer=tokenizer, max_tokens=512)
                    .explode()
                    .embed(embedder=SentenceTransformerEmbedder(model_name=model_name, batch_size=100))
                    .sketch(window=17)

                ds.write.weaviate(
                    wv_client_args=wv_client_args,
                    collection_name=collection,
                    collection_config=collection_config_params
                )
        """
        from sycamore.connectors.weaviate import (
            WeaviateDocumentWriter,
            WeaviateCrossReferenceWriter,
            WeaviateClientParams,
            WeaviateWriterTargetParams,
        )

        # Importing _ prefixed stuff is fairly common for users of the weaviate codebase
        from weaviate.collections.classes.config import _CollectionConfigCreate

        if collection_config is None:
            collection_config = dict()
        client_params = WeaviateClientParams(**wv_client_args)
        collection_config_object: _CollectionConfigCreate
        if "name" in collection_config:
            assert collection_config["name"] == collection_name
            collection_config_object = _CollectionConfigCreate(**collection_config)
        else:
            collection_config_object = _CollectionConfigCreate(name=collection_name, **collection_config)
        target_params = WeaviateWriterTargetParams(
            name=collection_name, collection_config=collection_config_object, flatten_properties=flatten_properties
        )

        wv_docs = WeaviateDocumentWriter(
            self.plan, client_params, target_params, name="weaviate_write_documents", **kwargs
        )
        wv_refs = WeaviateCrossReferenceWriter(
            wv_docs, client_params, target_params, name="weaviate_write_references", **kwargs
        )

        wvds = DocSet(self.context, wv_refs)
        if execute:
            # If execute, force execution
            wvds.execute()
            return None
        else:
            return wvds

    @requires_modules("pinecone", extra="pinecone")
    def pinecone(
        self,
        *,
        index_name: str,
        index_spec: Optional[Any] = None,
        namespace: str = "",
        dimensions: Optional[int] = None,
        distance_metric: str = "cosine",
        api_key: Optional[str] = None,
        execute: bool = True,
        log: bool = False,
        **kwargs,
    ) -> Optional["DocSet"]:
        """Writes the content of the DocSet into a Pinecone vector index.

        Args:
            index_name: Name of the pinecone index to ingest into
            index_spec: Cloud parameters needed by pinecone to create your index. See
                    https://docs.pinecone.io/guides/indexes/create-an-index
                    Defaults to None, which assumes the index already exists, and
                    will not modify an existing index if provided
            namespace: Namespace withing the pinecone index to ingest into. See
                    https://docs.pinecone.io/guides/indexes/use-namespaces
                    Defaults to "", which is the default namespace
            dimensions: Dimensionality of dense vectors in your index.
                    Defaults to None, which assumes the index already exists, and
                    will not modify an existing index if provided.
            distance_metric: Distance metric used for nearest-neighbor search in your index.
                    Defaults to "cosine", but will not modify an already-existing index
            api_key: Pinecone service API Key. Defaults to None (will use the environment
                    variable PINECONE_API_KEY).
            kwargs: Arguments to pass to the underlying execution engine

        Example:
            The following shows how to read a pdf dataset into a ``DocSet`` and write it out
            to a pinecone index called "mytestingindex"

            .. code-block:: python

                model_name = "sentence-transformers/all-MiniLM-L6-v2"
                tokenizer = HuggingFaceTokenizer(model_name)
                ctx = sycamore.init()
                ds = (
                    ctx.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=ArynPartitioner(extract_table_structure=True, extract_images=True))
                    .explode()
                    .embed(embedder=SentenceTransformerEmbedder(model_name=model_name, batch_size=100))
                    .term_frequency(tokenizer=tokenizer, with_token_ids=True)
                    .sketch(window=17)
                )

                ds.write.pinecone(
                    index_name="mytestingindex",
                    index_spec=pinecone.ServerlessSpec(cloud="aws", region="us-east-1"),
                    namespace="",
                    dimensions=384,
                    distance_metric="dotproduct",
                )

        """
        from sycamore.connectors.pinecone import (
            PineconeWriter,
            PineconeWriterClientParams,
            PineconeWriterTargetParams,
        )
        import os

        if log:
            logger.setLevel(20)
        if api_key is None:
            api_key = os.environ.get("PINECONE_API_KEY", "")
        assert (
            api_key is not None and len(api_key) != 0
        ), "Missing api key: either provide it as an argument or set the PINECONE_API_KEY env variable."
        pcp = PineconeWriterClientParams(api_key=api_key)
        ptp = PineconeWriterTargetParams(
            index_name=index_name,
            namespace=namespace,
            index_spec=index_spec,
            dimensions=dimensions,
            distance_metric=distance_metric,
        )

        pc = PineconeWriter(self.plan, client_params=pcp, target_params=ptp, name="pinecone_write", **kwargs)
        pcds = DocSet(self.context, pc)
        if execute:
            # If execute, force execution
            pcds.execute()
            return None
        else:
            return pcds

    @requires_modules("duckdb", extra="duckdb")
    def duckdb(
        self,
        dimensions: int,
        db_url: Optional[str] = None,
        table_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        schema: Optional[dict[str, str]] = None,
        execute: bool = True,
        **kwargs,
    ):
        """
        Writes the content of the DocSet into a DuckDB database.

        Args:
            dimensions: The dimensions of the embeddings of each vector (required paramater)
            db_url: The URL of the DuckDB database.
            table_name: The table name to write the data to when possible
            batch_size: The file batch size when loading entries into the DuckDB database table
            schema: Defines the schema of the table to enter entries
            execute: Flag that determines whether to execute immediately

        Example:
            The following shows how to read a pdf dataset into a ``DocSet`` and write it out
            to a DuckDB database and read from it.

            .. code-block:: python
                table_name = "duckdb_table"
                db_url = "tmp.db"
                model_name = "sentence-transformers/all-MiniLM-L6-v2"
                paths = str(TEST_DIR / "resources/data/pdfs/")

                OpenAI(OpenAIModels.GPT_3_5_TURBO_INSTRUCT.value)
                tokenizer = HuggingFaceTokenizer(model_name)

                ctx = sycamore.init()

                ds = (
                    ctx.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .regex_replace(COALESCE_WHITESPACE)
                    .mark_bbox_preset(tokenizer=tokenizer)
                    .merge(merger=MarkedMerger())
                    .spread_properties(["path"])
                    .split_elements(tokenizer=tokenizer, max_tokens=512)
                    .explode()
                    .embed(embedder=SentenceTransformerEmbedder(model_name=model_name, batch_size=100))
                )
                ds.write.duckdb(table_name=table_name, db_url=db_url)
        """
        from sycamore.connectors.duckdb.duckdb_writer import (
            DuckDBWriter,
            DuckDBWriterClientParams,
            DuckDBWriterTargetParams,
        )

        client_params = DuckDBWriterClientParams()
        target_params = DuckDBWriterTargetParams(
            **{
                k: v
                for k, v in {
                    "db_url": db_url,
                    "table_name": table_name,
                    "batch_size": batch_size,
                    "schema": schema,
                    "dimensions": dimensions,
                }.items()
                if v is not None
            }  # type: ignore
        )
        from ray.data import ActorPoolStrategy

        kwargs["compute"] = ActorPoolStrategy(size=1)
        ddb = DuckDBWriter(
            self.plan,
            client_params=client_params,
            target_params=target_params,
            name="duckdb_write_documents",
            **kwargs,
        )
        ddbds = DocSet(self.context, ddb)
        if execute:
            ddbds.execute()
            return None
        else:
            return ddbds

    @requires_modules("elasticsearch", extra="elasticsearch")
    def elasticsearch(
        self,
        *,
        url: str,
        index_name: str,
        es_client_args: dict = {},
        wait_for_completion: str = "false",
        settings: Optional[dict] = None,
        mappings: Optional[dict] = None,
        execute: bool = True,
        **kwargs,
    ) -> Optional["DocSet"]:
        """Writes the content of the DocSet into the specified Elasticsearch index.

        Args:
            url: Connection endpoint for the Elasticsearch instance. Note that this must be paired with the
                necessary client arguments below
            index_name: Index name to write to in the Elasticsearch instance
            es_client_args: Authentication arguments to be specified (if needed). See more information at
                https://elasticsearch-py.readthedocs.io/en/v8.14.0/api/elasticsearch.html
            wait_for_completion: Whether to wait for completion of the write before proceeding with next steps.
                See more information at https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-refresh.html
            mappings: Mapping of the Elasticsearch index, can be optionally specified
            settings: Settings of the Elasticsearch index, can be optionally specified
            execute: Execute the pipeline and write to weaviate on adding this operator. If False,
                will return a DocSet with this write in the plan. Default is True
        Example:
            The following code shows how to read a pdf dataset into a ``DocSet`` and write it out to a
            local Elasticsearch index called `test-index`.

            .. code-block:: python

                url = "http://localhost:9201"
                index_name = "test-index"
                model_name = "sentence-transformers/all-MiniLM-L6-v2"
                paths = str(TEST_DIR / "resources/data/pdfs/")

                OpenAI(OpenAIModels.GPT_3_5_TURBO_INSTRUCT.value)
                tokenizer = HuggingFaceTokenizer(model_name)

                ctx = sycamore.init()

                ds = (
                    ctx.read.binary(paths, binary_format="pdf")
                    .partition(partitioner=UnstructuredPdfPartitioner())
                    .regex_replace(COALESCE_WHITESPACE)
                    .mark_bbox_preset(tokenizer=tokenizer)
                    .merge(merger=MarkedMerger())
                    .spread_properties(["path"])
                    .split_elements(tokenizer=tokenizer, max_tokens=512)
                    .explode()
                    .embed(embedder=SentenceTransformerEmbedder(model_name=model_name, batch_size=100))
                    .sketch(window=17)
                )
                ds.write.elasticsearch(url=url, index_name=index_name)
        """
        from sycamore.connectors.elasticsearch import (
            ElasticsearchDocumentWriter,
            ElasticsearchWriterClientParams,
            ElasticsearchWriterTargetParams,
        )

        client_params = ElasticsearchWriterClientParams(url=url, es_client_args=es_client_args)
        target_params = ElasticsearchWriterTargetParams(
            index_name=index_name,
            wait_for_completion=wait_for_completion,
            **{
                k: v
                for k, v in {
                    "mappings": mappings,
                    "settings": settings,
                }.items()
                if v is not None
            },  # type: ignore
        )
        es_docs = ElasticsearchDocumentWriter(
            self.plan, client_params, target_params, name="elastic_document_writer", **kwargs
        )
        esds = DocSet(self.context, es_docs)
        if execute:
            # If execute, force execution
            esds.execute()
            return None
        else:
            return esds

    @requires_modules("neo4j", extra="neo4j")
    def neo4j(
        self,
        uri: str,
        auth: Union[tuple[Any, Any], "Auth", "AuthManager", None],
        import_dir: str,
        database: str = "neo4j",
        use_auradb: bool = False,
        s3_session: Optional[Session] = None,
        **kwargs,
    ) -> Optional["DocSet"]:
        """
        ***EXPERIMENTAL***

        Writes the content of the DocSet into the specified Neo4j database.

        Args:
            uri: Connection endpoint for the neo4j instance. Note that this must be paired with the
                necessary client arguments below
            auth: Authentication arguments to be specified. See more information at
                https://neo4j.com/docs/api/python-driver/current/api.html#auth-ref
            database: database to write to in Neo4j. By default in the neo4j community addition, new databases
                cannot be instantiated so you must use "neo4j". If using enterprise edition, ensure the database exists.
            import_dir: the import directory that neo4j uses. You can specify where to mount this volume when you launch
                your neo4j docker container.
        Example:
            The following code shows how to write to a neo4j database

            ..code-block::python
            URI = "neo4j://localhost:7687"
            AUTH = ("neo4j", "xxxxx")

            metadata = [GraphMetadata(nodeKey='company',nodeLabel='Company',relLabel='FILED_BY'),
                        GraphMetadata(nodeKey='gics_sector',nodeLabel='Sector',relLabel='IN_SECTOR'),
                        GraphMetadata(nodeKey='doc_type',nodeLabel='Document Type',relLabel='IS_TYPE'),
                        GraphMetadata(nodeKey='doc_period',nodeLabel='Year',relLabel='FILED_DURING'),
                        ]

            ds = (
                ctx.read.manifest(...)
                .partition(...)
                .extract_graph_structure([MetadataExtractor(metadata=metadata)])
                .explode()
            )

            ds.write.neo4j(uri=URI,auth=AUTH,database="neo4j",import_dir="/home/admin/neo4j/import")
            .. code-block:: python
        """
        import os
        from mypy_boto3_s3.client import S3Client
        from mypy_boto3_s3.service_resource import S3ServiceResource
        from sycamore.connectors.neo4j import (
            Neo4jWriterClientParams,
            Neo4jWriterTargetParams,
            Neo4jValidateParams,
        )
        from sycamore.plan_nodes import Node
        from sycamore.connectors.neo4j import Neo4jPrepareCSV, Neo4jWriteCSV, Neo4jLoadCSV
        from sycamore.connectors.neo4j.neo4j_writer import (
            create_temp_bucket,
            delete_temp_bucket,
            load_to_s3_bucket,
            get_neo4j_import_info,
        )
        import time

        if kwargs.get("execute") is False:
            raise NotImplementedError
        if self.context.exec_mode != ExecMode.RAY:
            raise NotImplementedError
        if use_auradb and not s3_session:
            raise ValueError("If using AuraDB, you must also pass in a s3_session object to temporarily host files.")

        class Wrapper(Node):
            def __init__(self, dataset):
                self._ds = dataset

            def execute(self, **kwargs):
                return self._ds

        import_dir = os.path.expanduser(import_dir)
        client_params = Neo4jWriterClientParams(uri=uri, auth=auth, import_dir=import_dir)
        target_params = Neo4jWriterTargetParams(database=database)
        Neo4jValidateParams(client_params=client_params, target_params=target_params)

        # Execute the docset up until this point and store it in a node
        pre_n4j_plan = Execution(self.context)._apply_rules(self.plan)
        pnjds = Execution(self.context)._execute_ray(pre_n4j_plan)
        pnjds = pnjds.materialize()
        self.plan = Wrapper(pnjds)
        pre_n4j_plan.traverse(visit=lambda n: n.finalize())

        start = time.time()
        Neo4jPrepareCSV(plan=self.plan, client_params=client_params)
        Neo4jWriteCSV(plan=self.plan, client_params=client_params).execute().materialize()
        end = time.time()
        logger.info(f"TIME TAKEN TO WRITE CSV: {end-start} SECONDS")

        nodes, relationships, labels = get_neo4j_import_info(import_dir=import_dir)
        # If using auradb, load to files to s3

        s3_client: S3Client
        s3_resource: S3ServiceResource
        s3_bucket: str
        if use_auradb:
            assert s3_session is not None
            s3_client = s3_session.client("s3")
            s3_resource = s3_session.resource("s3")
            s3_bucket = create_temp_bucket(s3_client=s3_client)
            nodes, relationships = load_to_s3_bucket(s3_client=s3_client, bucket_name=s3_bucket, import_dir=import_dir)

        import_paths = {"nodes": nodes, "relationships": relationships, "labels": labels}

        Neo4jLoadCSV(client_params=client_params, target_params=target_params, import_paths=import_paths)

        # cleanup s3 files if using auradb
        if use_auradb:
            delete_temp_bucket(s3_client, s3_resource, s3_bucket)

        return None

    def files(
        self,
        path: str,
        filesystem: Optional[FileSystem] = None,
        filename_fn: Callable[[Document], str] = default_filename,
        doc_to_bytes_fn: Callable[[Document], bytes] = default_doc_to_bytes,
        **resource_args,
    ) -> None:
        """Writes the content of each Document to a separate file.

        Args:
            path: The path prefix to write to. Should include the scheme if not local.
            filesystem: The pyarrow.fs FileSystem to use.
            filename_fn: A function for generating a file name. Takes a Document
                and returns a unique name that will be appended to path.
            doc_to_bytes_fn: A function from a Document to bytes for generating the data to write.
                Defaults to using text_representation if available, or binary_representation
                if not.
            resource_args: Arguments to pass to the underlying execution environment.
        """
        file_writer: Node = FileWriter(
            self.plan,
            path,
            filesystem=filesystem,
            filename_fn=filename_fn,
            doc_to_bytes_fn=doc_to_bytes_fn,
            **resource_args,
        )
        file_writer = Execution(self.context)._apply_rules(file_writer)
        file_writer.execute()
        file_writer.traverse(visit=lambda n: n.finalize())

    def json(
        self,
        path: str,
        filesystem: Optional[FileSystem] = None,
        **resource_args,
    ) -> None:
        """
        Writes Documents in JSONL format to files, one file per
        block.  Typically, a block corresponds to a single
        pre-explode source document.

        Args:
            path: The path prefix to write to. Should include the scheme if not local.
            filesystem: The pyarrow.fs FileSystem to use.
            resource_args: Arguments to pass to the underlying execution environment.
        """

        node: Node = JsonWriter(self.plan, path, filesystem=filesystem, **resource_args)
        node = Execution(self.context)._apply_rules(node)
        node.execute()
        node.traverse(visit=lambda n: n.finalize())
