from abc import abstractmethod
from typing import Any, Optional, List, Dict, Tuple

from sycamore.functions.basic_filters import MatchFilter, RangeFilter
from sycamore.llms.prompts.default_prompts import EntityExtractorMessagesPrompt, LlmFilterMessagesPrompt
from sycamore.query.execution.metrics import SycamoreQueryLogger
from sycamore.query.operators.count import Count
from sycamore.query.operators.basic_filter import BasicFilter
from sycamore.query.operators.limit import Limit
from sycamore.query.operators.llm_extract_entity import LlmExtractEntity
from sycamore.query.operators.llm_filter import LlmFilter
from sycamore.query.operators.summarize_data import SummarizeData
from sycamore.query.operators.query_database import QueryDatabase
from sycamore.query.operators.top_k import TopK
from sycamore.query.operators.field_in import FieldIn
from sycamore.query.operators.sort import Sort

from sycamore.query.execution.operations import (
    summarize_data,
)
from sycamore.llms import OpenAI, OpenAIModels
from sycamore.transforms.extract_entity import OpenAIEntityExtractor
from sycamore.utils.cache import S3Cache

from sycamore import DocSet, Context
from sycamore.query.operators.logical_operator import LogicalOperator
from sycamore.query.execution.physical_operator import PhysicalOperator, get_var_name, get_str_for_dict


class SycamoreOperator(PhysicalOperator):
    """
    This interface is a Sycamore platform implementation of a Logical Operator generated by the query planner.
    It serves 2 purposes:
    1. Execute the node using Sycamore tools (possibly lazy)
    2. Return a python script in string form that can be run to achieve the same result

    Args:
        context (Context): The Sycamore context to use.
        logical_node (Operator): The logical query plan node to execute. Contains runtime params based on type.
        query_id (str): Query id
        inputs (List[Any]): List of inputs required to execute the node. Varies based on node type.
    """

    def __init__(
        self,
        context: Context,
        logical_node: LogicalOperator,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(logical_node, query_id, inputs)
        self.context = context
        self.trace_dir = trace_dir

    @abstractmethod
    def execute(self) -> Any:
        """
        execute the node
        :return: execution result, can be a Lazy DocSet plan, or executed result like a integer (for count)
        """
        pass

    @abstractmethod
    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        pass

    def get_node_args(self) -> Dict:
        return {"name": str(self.logical_node.node_id)}

    def get_execute_args(self) -> Dict:
        intermediate_datasink_kwargs: Dict[str, Any] = {
            "query_id": self.query_id,
            "node_id": self.logical_node.node_id,
            "path": "none",
        }
        if self.trace_dir:
            intermediate_datasink_kwargs.update(
                {"makedirs": True, "verbose": True, "path": f"{self.trace_dir}/{self.query_id}/"}
            )
        args = {
            "write_intermediate_data": True,
            "intermediate_datasink": SycamoreQueryLogger,
            "intermediate_datasink_kwargs": intermediate_datasink_kwargs,
        }
        args.update(self.get_node_args())
        return args


class SycamoreQueryDatabase(SycamoreOperator):
    """
    Currently only supports an OpenSearch scan load implementation.
    Args:
        os_client_args (dict): OpenSearch client args passed to OpenSearchScan to initialize the client.
    """

    def __init__(
        self,
        context: Context,
        logical_node: QueryDatabase,
        query_id: str,
        os_client_args: Dict,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(context=context, logical_node=logical_node, query_id=query_id, trace_dir=trace_dir)
        self.os_client_args = os_client_args

    def execute(self) -> Any:
        assert isinstance(self.logical_node, QueryDatabase)
        result = self.context.read.opensearch(os_client_args=self.os_client_args, index_name=self.logical_node.index)
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        assert isinstance(self.logical_node, QueryDatabase)
        return (
            f"""
os_client_args = {self.os_client_args}
{output_var or get_var_name(self.logical_node)} = context.read.opensearch(
    os_client_args=os_client_args,
    index_name='{self.logical_node.index}'
)
""",
            [],
        )


class SycamoreSummarizeData(SycamoreOperator):
    """
    Use an LLM to generate a response based on the user input question and provided result set.
    Args:
        s3_cache_path (str): Optional S3 path to use for caching
    """

    def __init__(
        self,
        context: Context,
        logical_node: SummarizeData,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
        s3_cache_path: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)
        self.s3_cache_path = s3_cache_path
        assert isinstance(self.logical_node, SummarizeData)

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) >= 1, "SummarizeData requires at least 1 input node"
        assert isinstance(self.logical_node, SummarizeData)
        question = self.logical_node.question
        assert question is not None and isinstance(question, str)
        description = self.logical_node.description
        assert description is not None and isinstance(description, str)
        result = summarize_data(
            llm=OpenAI(OpenAIModels.GPT_4O.value, cache=S3Cache(self.s3_cache_path) if self.s3_cache_path else None),
            question=question,
            result_description=description,
            result_data=self.inputs,
            **self.get_execute_args(),
        )
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        assert isinstance(self.logical_node, SummarizeData)
        question = self.logical_node.question
        description = self.logical_node.description
        assert self.logical_node.dependencies is not None and len(self.logical_node.dependencies) >= 1

        cache_string = ""
        if self.s3_cache_path:
            cache_string = f", cache=S3Cache('{self.s3_cache_path}')"
        logical_deps_str = ""
        for i, inp in enumerate(self.logical_node.dependencies):
            logical_deps_str += input_var or get_var_name(inp)
            if i != len(self.logical_node.dependencies) - 1:
                logical_deps_str += ", "

        result = f"""
{output_var or get_var_name(self.logical_node)} = summarize_data(
    llm=OpenAI(OpenAIModels.GPT_4O.value{cache_string}),
    question='{question}',
    result_description='{description}',
    result_data=[{logical_deps_str}]
)
result = {output_var or get_var_name(self.logical_node)}
print(result)
"""
        return result, [
            "from sycamore.query.execution.operations import summarize_data",
            "from sycamore.llms import OpenAI, OpenAIModels",
        ]


class SycamoreLlmFilter(SycamoreOperator):
    """
    Use an LLM to filter records on a Docset.
    Args:
        s3_cache_path (str): Optional S3 path to use for caching
    """

    def __init__(
        self,
        context: Context,
        logical_node: LlmFilter,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
        s3_cache_path: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)
        self.s3_cache_path = s3_cache_path

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "LlmFilter requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "LlmFilter requires a DocSet input"
        assert isinstance(self.logical_node, LlmFilter)
        question = self.logical_node.question
        field = self.logical_node.field

        # load into local vars for Ray serialization magic
        s3_cache_path = self.s3_cache_path

        prompt = LlmFilterMessagesPrompt(filter_question=question).get_messages_dict()

        result = self.inputs[0].llm_filter(
            llm=OpenAI(OpenAIModels.GPT_4O.value, cache=S3Cache(s3_cache_path) if s3_cache_path else None),
            new_field="_autogen_LLMFilterOutput",
            prompt=prompt,
            field=field,
            threshold=3,
            **self.get_node_args(),
        )
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        assert self.logical_node.dependencies is not None and len(self.logical_node.dependencies) == 1
        assert isinstance(self.logical_node, LlmFilter)
        cache_string = ""
        if self.s3_cache_path:
            cache_string = f", cache=S3Cache('{self.s3_cache_path}')"
        result = (
            f"prompt = LlmFilterMessagesPrompt(filter_question='{self.logical_node.question}').get_messages_dict()\n"
            f"{output_var or get_var_name(self.logical_node)} = "
            f"{input_var or get_var_name(self.logical_node.dependencies[0])}.llm_filter(\n"
            f"llm=OpenAI(OpenAIModels.GPT_4O.value{cache_string}),\n"
            "new_field='_autogen_LLMFilterOutput',\n"
            "prompt=prompt,\n"
            f"field='{self.logical_node.field}',\n"
            "threshold=3,\n"
            f"**{self.get_node_args()},\n"
            ")"
        )
        return result, [
            "from sycamore.llms import OpenAI, OpenAIModels",
            "from sycamore.llms.prompts.default_prompts import LlmFilterMessagesPrompt",
        ]


class SycamoreBasicFilter(SycamoreOperator):
    """
    Filter a DocSet
    """

    def __init__(
        self,
        context: Context,
        logical_node: BasicFilter,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "Filter requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "Filter requires a DocSet input"

        # Load into local vars for Ray serialization magic.
        logical_node = self.logical_node
        assert isinstance(logical_node, BasicFilter)

        if logical_node.range_filter:
            field = logical_node.field
            start = logical_node.start
            end = logical_node.end
            date = logical_node.date

            result = self.inputs[0].filter(
                f=RangeFilter(field=str(field), start=start, end=end, date=date), **self.get_node_args()
            )
        else:
            query = logical_node.query
            assert query is not None
            field = logical_node.field
            result = self.inputs[0].filter(f=MatchFilter(query=query, field=field), **self.get_node_args())
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        assert isinstance(self.logical_node, BasicFilter)
        assert self.logical_node.dependencies is not None and len(self.logical_node.dependencies) == 1
        imports: list[str] = []
        if self.logical_node.range_filter:
            field = self.logical_node.field
            start = self.logical_node.start
            assert start is None or isinstance(start, str)
            end = self.logical_node.end
            assert end is None or isinstance(end, str)
            date = self.logical_node.date

            script = (
                f"{output_var or get_var_name(self.logical_node)} = "
                f"{input_var or get_var_name(self.logical_node.dependencies[0])}.filter(\n"
                "f=RangeFilter("
                f"field='{field}',\n"
                f"start='{start}',\n"
                f"end='{end}',\n"
                f"date='{date}),'\n"
                f"**{self.get_node_args()})"
            )
            imports = ["from sycamore.functions.basic_filters import RangeFilter"]
        else:
            script = (
                f"{output_var or get_var_name(self.logical_node)} = "
                f"{input_var or get_var_name(self.logical_node.dependencies[0])}.filter(\n"
                "f=MatchFilter("
                f"query='{self.logical_node.query}',\n"
                f"field='{self.logical_node.field}',"
                f"**{self.get_node_args()})"
            )
            imports = ["from sycamore.functions.basic_filters import MatchFilter"]
        return script, imports


class SycamoreCount(SycamoreOperator):
    """
    Count documents in a DocSet. Can do a unique count optionally.
    """

    def __init__(
        self,
        context: Context,
        logical_node: Count,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "Count requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "Count requires a DocSet input"
        # load into local vars for Ray serialization magic
        logical_node = self.logical_node
        assert isinstance(logical_node, Count)
        field = logical_node.field
        primary_field = logical_node.primary_field

        if field is None and primary_field is None:
            result = self.inputs[0].count(**self.get_execute_args())
        else:
            field_name = field or primary_field
            assert isinstance(field_name, str)
            result = self.inputs[0].count_distinct(field=field_name, **self.get_execute_args())
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        assert isinstance(self.logical_node, Count)
        assert self.logical_node.dependencies is not None and len(self.logical_node.dependencies) == 1
        field = self.logical_node.field
        primary_field = self.logical_node.primary_field

        imports: list[str] = []
        script = f"""{output_var or get_var_name(self.logical_node)} ="""
        if field is None and primary_field is None:
            script += f"""{input_var or get_var_name(self.logical_node.dependencies[0])}.count("""
        else:
            script += f"""{input_var or get_var_name(self.logical_node.dependencies[0])}.count_distinct("""
            if field:
                script += f"""field='{field}', """
            elif primary_field:
                script += f"""field='{primary_field}', """
        script += f"""**{get_str_for_dict(self.get_execute_args())})"""
        return script, imports


class SycamoreLlmExtractEntity(SycamoreOperator):
    """
    Use an LLM to extract information from your data. The data is available for downstream tasks to consume.
    Args:
        s3_cache_path (str): Optional S3 path to use for caching
    """

    def __init__(
        self,
        context: Context,
        logical_node: LlmExtractEntity,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
        s3_cache_path: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)
        self.s3_cache_path = s3_cache_path

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "LlmExtractEntity requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "LlmExtractEntity requires a DocSet input"
        # load into local vars for Ray serialization magic
        s3_cache_path = self.s3_cache_path
        logical_node = self.logical_node
        assert isinstance(logical_node, LlmExtractEntity)
        question = logical_node.question
        new_field = logical_node.new_field
        field = logical_node.field
        fmt = logical_node.new_field_type
        discrete = logical_node.discrete

        prompt = EntityExtractorMessagesPrompt(
            question=question, field=field, format=fmt, discrete=discrete
        ).get_messages_dict()

        entity_extractor = OpenAIEntityExtractor(
            entity_name=new_field,
            llm=OpenAI(OpenAIModels.GPT_4O.value, cache=S3Cache(s3_cache_path) if s3_cache_path else None),
            use_elements=False,
            prompt=prompt,
            field=field,
        )
        result = self.inputs[0].extract_entity(entity_extractor=entity_extractor, **self.get_node_args())
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        logical_node = self.logical_node
        assert isinstance(logical_node, LlmExtractEntity)
        question = logical_node.question
        new_field = logical_node.new_field
        field = logical_node.field
        fmt = logical_node.new_field_type
        discrete = logical_node.discrete
        assert logical_node.dependencies is not None and len(logical_node.dependencies) == 1

        cache_string = ""
        if self.s3_cache_path:
            cache_string = f", cache=S3Cache('{self.s3_cache_path}')"
        result = f"""
        prompt = EntityExtractorMessagesPrompt(
                question='{question}', field='{field}', format='{fmt}, discrete={discrete}
            ).get_messages_dict()

        entity_extractor = OpenAIEntityExtractor(
            entity_name='{new_field}',
            llm=OpenAI(OpenAIModels.GPT_4O.value{cache_string}),
            use_elements=False,
            prompt=prompt,
            field='{field}',
        )
    {output_var or get_var_name(logical_node)} = 
        {input_var or get_var_name(logical_node.dependencies[0])}.extract_entity(
                entity_extractor=entity_extractor,
                **{self.get_node_args()}
            )
    """
        return result, [
            "from sycamore.llms.prompts.default_prompts import EntityExtractorMessagesPrompt",
            "from sycamore.transforms.extract_entity import OpenAIEntityExtractor",
            "from sycamore.llms import OpenAI, OpenAIModels",
        ]


class SycamoreSort(SycamoreOperator):
    """
    Sort a DocSet on a given key.
    """

    def __init__(
        self,
        context: Context,
        logical_node: Sort,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "Sort requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "Sort requires a DocSet input"

        # load into local vars for Ray serialization magic
        logical_node = self.logical_node
        assert isinstance(logical_node, Sort)
        descending = logical_node.descending
        field = logical_node.field
        default_value = logical_node.default_value

        result = self.inputs[0].sort(descending=descending, field=field, default_val=default_value)

        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        logical_node = self.logical_node
        assert isinstance(logical_node, Sort)
        descending = logical_node.descending
        field = logical_node.field
        default_value = logical_node.default_value
        assert logical_node.dependencies is not None and len(logical_node.dependencies) == 1

        result = f"""
{output_var or get_var_name(self.logical_node)} = {input_var or get_var_name(logical_node.dependencies[0])}.sort(
    descending={descending},
    field='{field}'
    default_val={default_value}
)
"""
        return result, []


class SycamoreTopK(SycamoreOperator):
    """
    Return the Top-K values from a DocSet
    Args:
        s3_cache_path (str): Optional S3 path to use for caching when using an LLM
    """

    def __init__(
        self,
        context: Context,
        logical_node: TopK,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
        s3_cache_path: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)
        self.s3_cache_path = s3_cache_path

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "TopK requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "TopK requires a DocSet input"
        # load into local vars for Ray serialization magic
        s3_cache_path = self.s3_cache_path
        logical_node = self.logical_node
        assert isinstance(logical_node, TopK)

        result = self.inputs[0].top_k(
            llm=OpenAI(OpenAIModels.GPT_4O.value, cache=S3Cache(s3_cache_path) if s3_cache_path else None),
            field=logical_node.field,
            k=logical_node.K,
            descending=logical_node.descending,
            llm_cluster=logical_node.llm_cluster,
            unique_field=logical_node.primary_field,
            llm_cluster_instruction=logical_node.llm_cluster_instruction,
            **self.get_execute_args(),
        )
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        logical_node = self.logical_node
        assert isinstance(logical_node, TopK)
        assert logical_node.dependencies is not None and len(logical_node.dependencies) == 1

        cache_string = ""
        if self.s3_cache_path:
            cache_string = f", cache=S3Cache('{self.s3_cache_path}')"
        result = f"""
{output_var or get_var_name(self.logical_node)} = {input_var or get_var_name(logical_node.dependencies[0])}.top_k(
    llm=OpenAI(OpenAIModels.GPT_4O.value{cache_string}),
    field='{logical_node.field}',
    k={logical_node.K},
    descending={logical_node.descending}',
    llm_cluster={logical_node.llm_cluster},
    unique_field='{logical_node.primary_field}',
    llm_cluster_instruction='{logical_node.llm_cluster_instruction}',
    **{self.get_execute_args()},
)
"""
        return result, [
            "from sycamore.llms import OpenAI, OpenAIModels",
        ]


class SycamoreFieldIn(SycamoreOperator):
    """
    Return 2 DocSets joined
    """

    def __init__(
        self,
        context: Context,
        logical_node: FieldIn,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(
            context=context, logical_node=logical_node, query_id=query_id, inputs=inputs, trace_dir=trace_dir
        )

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 2, "Join requires 2 input nodes"
        assert isinstance(self.inputs[0], DocSet) and isinstance(
            self.inputs[1], DocSet
        ), "Join requires 2 DocSet inputs"

        logical_node = self.logical_node
        assert isinstance(logical_node, FieldIn)
        field1 = logical_node.field_one
        field2 = logical_node.field_two

        result = self.inputs[0].field_in(
            docset2=self.inputs[1],
            field1=field1,
            field2=field2,
        )
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        logical_node = self.logical_node
        assert isinstance(logical_node, FieldIn)
        field1 = logical_node.field_one
        field2 = logical_node.field_two
        assert logical_node.dependencies is not None and len(logical_node.dependencies) == 2

        result = f"""
{output_var or get_var_name(self.logical_node)} = {input_var or get_var_name(logical_node.dependencies[0])}.field_in(
    docset2={input_var or get_var_name(logical_node.dependencies[2])},
    field1='{field1}',
    field2='{field2}'
)
"""
        return result, []


class SycamoreLimit(SycamoreOperator):
    """
    Limit the number of results on a DocSet
    """

    def __init__(
        self,
        context: Context,
        logical_node: Limit,
        query_id: str,
        inputs: Optional[List[Any]] = None,
        trace_dir: Optional[str] = None,
    ) -> None:
        super().__init__(context, logical_node, query_id, inputs, trace_dir=trace_dir)

    def execute(self) -> Any:
        assert self.inputs and len(self.inputs) == 1, "Limit requires 1 input node"
        assert isinstance(self.inputs[0], DocSet), "Limit requires a DocSet input"

        # load into local vars for Ray serialization magic
        logical_node = self.logical_node
        assert isinstance(logical_node, Limit)
        result = self.inputs[0].limit(logical_node.num_records)
        return result

    def script(self, input_var: Optional[str] = None, output_var: Optional[str] = None) -> Tuple[str, List[str]]:
        logical_node = self.logical_node
        assert isinstance(logical_node, Limit)
        assert logical_node.dependencies is not None and len(logical_node.dependencies) == 1

        result = f"""
{output_var or get_var_name(logical_node)} = {input_var or get_var_name(logical_node.dependencies[0])}.limit(
    {logical_node.num_records},
    **{self.get_execute_args()},
)
"""
        return result, []
