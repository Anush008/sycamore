from sycamore.context import Context
from sycamore.query.client import SycamoreQueryClient


def get_custom_sycamore_query_client(
    llm=None, s3_cache_path=None, os_client_args=None, trace_dir=None, **kwargs
) -> Context:
    return SycamoreQueryClient(s3_cache_path=s3_cache_path, os_client_args=os_client_args, trace_dir=trace_dir)
