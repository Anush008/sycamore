#!/usr/bin/python3

import urllib.request
import ssl
import json
from pathlib import Path
import logging
import re
import time
import os
import sys

OPENSEARCH_URL = "https://localhost:9200"
ARYN_STATUSDIR = Path("/usr/share/opensearch/data/aryn_status")
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE
ML_CONFIG_FILE = ARYN_STATUSDIR / "ml_config.json"

if int(os.environ.get("DEBUG", 0)) > 0:
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
else:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)


def os_request_to_json(endpoint: str, body=None, method="GET"):
    """
    Send a request to opensearch.
    endpoint is just the bit after the opensearch url
    """
    assert endpoint[0] == "/", f"endpoint ({endpoint}) must include the initial /"
    url = OPENSEARCH_URL + endpoint

    x = None
    if body is None:
        logging.debug(f"{method} {endpoint}")
        x = urllib.request.urlopen(url=urllib.request.Request(url=url, method=method), context=SSL_CTX)
    else:
        logging.debug(f"{method} {endpoint} {body}")
        x = urllib.request.urlopen(
            url=urllib.request.Request(
                url=url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method=method,
            ),
            context=SSL_CTX,
        )
    return json.load(x)


def die(error_msg: str):
    logging.error(error_msg)
    exit()


def register_model_group():
    """
    register the aryn model group
    """
    model_group_body = {
        "name": "conversational_search_models",
        "description": "Public model group of the conversational search models we use",
    }
    id_maybe = model_group_exists(model_group_body["name"])
    if id_maybe is not None:
        return id_maybe
    model_group_result = os_request_to_json("/_plugins/_ml/model_groups/_register", model_group_body, "POST")
    with open(ARYN_STATUSDIR / "request.model_group", "w") as f:
        json.dump(model_group_result, f)
    if "model_group_id" not in model_group_result:
        die(f"Model group id not found: {model_group_result}")
    return model_group_result["model_group_id"]


def create_connector(connector_body, connector_name, attempts=15):
    """
    create an HTTP connector. handle any errors appropriately
    """
    if "name" not in connector_body:
        die(f"missing 'name' field in connector body: {connector_body}")
    id_maybe = connector_exists(connector_body["name"])
    if id_maybe is not None:
        return id_maybe
    for i in range(attempts):
        connector_response = os_request_to_json("/_plugins/_ml/connectors/_create", connector_body, "POST")
        with open(ARYN_STATUSDIR / f"request.create_{connector_name}", "w") as f:
            json.dump(connector_response, f)
        wait_time, successful, connector_id = handle_connector_response(connector_response)
        if successful:
            return connector_id
        time.sleep(wait_time)
    die(f"Failed to create connector after {attempts} attempts")


def handle_connector_response(connector_response):
    """
    handle a create connector response, including errors
    """
    if "connector_id" in connector_response:
        return 0, True, connector_response["connector_id"]
    die(f"Error creating a connector: {connector_response}")


def cycle_task(try_again, get_action, task_id=None, timeout=60):
    """
    Spawn a task and watch it until it completes, handling errors and retrying
    - task_id: task id to watch
    - try_again: lambda that spawns a task or gets the running task (returns new task id)
    - get_action: lambda that decides what to do based on task status (wait n, return value, try again)
    """
    if task_id is None:
        task_id = try_again()
    delay = 0
    while delay < timeout:
        return_value, wait_time, should_try_again = get_action(task_id)
        if return_value is not None:
            return return_value
        if should_try_again:
            task_id = try_again()
        delay += wait_time
        time.sleep(wait_time)
    die(f"Could not complete task in {timeout} seconds")


def construct_get_action_fn(handle_error_fn, is_complete_fn=lambda x: False):
    """
    Construct the lambda to hand to `cycle_task` as `get_action`
    """

    def inner_get_action(task_id):
        task_status = os_request_to_json(f"/_plugins/_ml/tasks/{task_id}")
        if "state" not in task_status:
            die(f"task status missing 'state' field: {task_status}")
        state = task_status["state"]
        if state in ["RUNNING", "CREATED"]:
            # don't return anything, wait a sec, don't try again
            return None, 1, False
        elif state in ["COMPLETED"] or is_complete_fn():
            # return model id, no waiting, don't try again
            return task_status["model_id"], 0, False
        elif state in ["FAILED"]:
            # defer to error handling function
            return handle_error_fn(task_status)
        else:
            die(f"Unrecognized task status: {task_status}")

    return inner_get_action


def construct_error_handler_fn():
    """
    Construct the lambda to handle deployment errors
    """

    def inner_handle_deploy_error(task_status):
        """
        handle and error from deploying a model
        """
        logging.warning(f"Error detected: {task_status}")
        error_message = get_error_message(task_status)
        if error_message is None:
            die(f"Could not find error message, but FAILED state was found: {task_status}")
        if re.match(".*Memory.*Circuit.*Breaker.*", error_message):
            logging.info("Memory Circuit Breaker exception. Wait 5s while GC runs")
            return None, 5, True
        elif error_message == "Duplicate deploy model task":
            # Get running task logic is relocated to deploy function
            logging.info("Duplicate deploy model task. Try again")
            return None, 1, True
        elif re.match(".*OrtEnvironment.*", error_message):
            logging.info("Ort Environment error. Try again.")
            return None, 1, True
        else:
            die(f"Unrecognized error message: {error_message}")

    return inner_handle_deploy_error


def construct_deploy_try_again_fn(model_id):
    """
    Construct the lambda to hand to `cycle_task` as `try_again`
    """

    def inner_try_again():
        deploy_tasks = os_request_to_json(
            endpoint="/_plugins/_ml/tasks/_search",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"model_id": model_id}},
                            {"terms": {"state": ["RUNNING", "CREATED"]}},
                        ]
                    }
                },
                "sort": [{"create_time": {"order": "DESC"}}],
            },
            method="POST",
        )
        if deploy_tasks["hits"]["total"]["value"] != 0:
            return deploy_tasks["hits"]["hits"][0]["_id"]
        deploy_model_response = os_request_to_json(f"/_plugins/_ml/models/{model_id}/_deploy", method="POST")
        if "task_id" not in deploy_model_response:
            die(f"deploy model failed somehow: {deploy_model_response}")
        return deploy_model_response["task_id"]

    return inner_try_again


def construct_register_try_again_fn(register_model_body, model_name):
    def inner_try_again():
        """
        create a task in opensearch to register a model. Polling the task is handled elsewhere
        """
        register_model_response = os_request_to_json("/_plugins/_ml/models/_register", register_model_body, "POST")
        with open(ARYN_STATUSDIR / f"request.register_{model_name}", "w") as f:
            json.dump(register_model_response, f)
        if "task_id" not in register_model_response:
            die(f"register model failed somehow: {register_model_response}")
        return register_model_response["task_id"]

    return inner_try_again


def get_error_message(task_status):
    """
    Return the error message from a task status, or None if there is no error
    """
    if "error" not in task_status:
        return None
    else:
        error_message = str(task_status["error"])
        if error_message[0] == "{":
            error_message = str(list(json.loads(error_message).values())[0])
        return error_message


def model_is_deployed(model_id):
    """
    Use the GetModel api to determine whether a model is deployed
    """
    model_info = os_request_to_json(f"/_plugins/_ml/models/{model_id}")
    if "model_state" not in model_info:
        logging.warning(f"'model_state' not found for model {model_id}")
        return False
    return model_info["model_state"] == "DEPLOYED"


def construct_model_is_deplyed_fn(model_id):
    """
    Construct a lambda that calls model_is_deployed
    """

    def inner():
        return model_is_deployed(model_id)

    return inner


def get_model_id(official_model_name):
    """
    Use the SearchModel API to determine whether a model has been downloaded to the node.
    Return its id if found
    """
    search_request = {
        "query": {
            "bool": {
                "must_not": [{"exists": {"field": "chunk_number"}}],
                "must": [{"term": {"name.keyword": official_model_name}}],
            }
        }
    }
    response = os_request_to_json("/_plugins/_ml/models/_search", search_request, "POST")
    if response["hits"]["total"]["value"] > 0:
        return response["hits"]["hits"][0]["_id"]
    else:
        return None


def construct_get_model_id_fn(official_model_name):
    """
    Construct a lambda that calls get_model_id
    """

    def inner():
        return get_model_id(official_model_name)

    return inner


def connector_exists(official_connector_name):
    """
    Use the SearchConnecotrs API to determine whether a connector has been created
    Return its id if found
    """
    search_request = {"query": {"term": {"name.keyword": official_connector_name}}}
    response = os_request_to_json("/_plugins/_ml/connectors/_search", search_request, "POST")
    if response["hits"]["total"]["value"] > 0:
        return response["hits"]["hits"][0]["_id"]
    else:
        return None


def model_group_exists(model_group_name):
    """
    Use the SearchModelGroup API to determine whether the model group exists
    Return its id if found
    """
    search_request = {"query": {"term": {"name.keyword": model_group_name}}}
    response = os_request_to_json("/_plugins/_ml/model_groups/_search", search_request, "POST")
    if response["hits"]["total"]["value"] > 0:
        return response["hits"]["hits"][0]["_id"]
    else:
        return None


def setup_model(register_model_body, model_name, register_timeout=60, deploy_timeout=60):
    if "name" not in register_model_body:
        die(f"`name` key not found in register body: {register_model_body}")
    get_id = construct_get_model_id_fn(register_model_body["name"])
    model_id = get_id()
    if model_id is None:
        register_try_again = construct_register_try_again_fn(
            register_model_body=register_model_body, model_name=model_name
        )
        register_get_action = construct_get_action_fn(construct_error_handler_fn(), get_id)
        model_id = cycle_task(try_again=register_try_again, get_action=register_get_action, timeout=register_timeout)

    is_deployed = construct_model_is_deplyed_fn(model_id)
    deployed_model_id = model_id
    if not is_deployed():
        deploy_try_again = construct_deploy_try_again_fn(model_id)
        deploy_get_action = construct_get_action_fn(construct_error_handler_fn(), is_deployed)
        deployed_model_id = cycle_task(try_again=deploy_try_again, get_action=deploy_get_action, timeout=deploy_timeout)
    if model_id != deployed_model_id:
        die(f"Registered and Deployed model ids were different: [{model_id}] vs [{deployed_model_id}]")
    return model_id


def setup_embedding_model(model_group_id):
    """
    Register and deploy miniLM
    """
    model_name = "embedding"
    register_body = {
        "name": "sentence-transformers/all-MiniLM-L6-v2",
        "version": "1.0.1",
        "description": "This is a sentence-transformers model: It maps \
sentences & paragraphs to a 384 dimensional dense vector space and can be \
used for tasks like clustering or semantic search.",
        "model_task_type": "TEXT_EMBEDDING",
        "model_format": "ONNX",
        "model_group_id": model_group_id,
        "model_content_size_in_bytes": 91707516,
        "model_content_hash_value": "61ebca09b70c3061726fcd439dde0ad64ede6c5698cb30b594cb11b02603d64b",
        "model_config": {
            "model_type": "bert",
            "embedding_dimension": 384,
            "framework_type": "huggingface_transformers",
            "pooling_mode": "MEAN",
            "normalize_result": True,
            "all_config": '{"_name_or_path":"nreimers/MiniLM-L6-H384-uncased",\
"architectures":["BertModel"],"attention_probs_dropout_prob":0.1,"gradient_checkpointing"\
:false,"hidden_act":"gelu","hidden_dropout_prob":0.1,"hidden_size":384,"initializer_range"\
:0.02,"intermediate_size":1536,"layer_norm_eps":1e-12,"max_position_embeddings":512,\
"model_type":"bert","num_attention_heads":12,"num_hidden_layers":6,"pad_token_id":0,\
"position_embedding_type":"absolute","transformers_version":"4.8.2","type_vocab_size"\
:2,"use_cache":true,"vocab_size":30522}',
        },
        "created_time": 1676329221436,
        "url": "https://artifacts.opensearch.org/models/ml-models/huggingface/sentence-\
transformers/all-MiniLM-L6-v2/1.0.1/onnx/sentence-transformers_all-MiniLM-L6-v2-1.0.1-onnx.zip",
    }
    return setup_model(register_body, model_name)


def setup_reranking_model(model_group_id):
    """
    Register and deploy bge-reranker-base-quantized
    """
    model_name = "reranking"
    all_config = {
        "_name_or_path": "BAAI/bge-reranker-base",
        "architectures": ["XLMRobertaForSequenceClassification"],
        "attention_probs_dropout_prob": 0.1,
        "bos_token_id": 0,
        "classifier_dropout": None,
        "eos_token_id": 2,
        "hidden_act": "gelu",
        "hidden_dropout_prob": 0.1,
        "hidden_size": 768,
        "id2label": {"0": "LABEL_0"},
        "initializer_range": 0.02,
        "intermediate_size": 3072,
        "label2id": {"LABEL_0": 0},
        "layer_norm_eps": 1e-05,
        "max_position_embeddings": 514,
        "model_type": "xlm-roberta",
        "num_attention_heads": 12,
        "num_hidden_layers": 12,
        "output_past": True,
        "pad_token_id": 1,
        "position_embedding_type": "absolute",
        "torch_dtype": "float32",
        "transformers_version": "4.33.3",
        "type_vocab_size": 1,
        "use_cache": True,
        "vocab_size": 250002,
    }
    register_body = {
        "name": "BAAI/bge-reranker-base-quantized",
        "version": "1.0.0",
        "description": "Cross Encoder text similarity model",
        "model_format": "ONNX",
        "function_name": "TEXT_SIMILARITY",
        "model_group_id": model_group_id,
        "model_content_hash_value": "04157d66d847d08b3d2b51ad36cf0e1fb82afadb8086212a1d2bac2b7d6fe08a",
        "model_config": {
            "model_type": "roberta",
            "embedding_dimension": 1,
            "framework_type": "huggingface_transformers",
            "all_config": json.dumps(all_config),
        },
        "url": "https://aryn-public.s3.amazonaws.com/models/BAAI/bge-reranker-base-quantized-2.zip",
    }
    return setup_model(register_body, model_name, register_timeout=120)


def setup_openai_model(model_group_id):
    """
    Create openai connector, register and deploy remote model
    """
    if "OPENAI_API_KEY" not in os.environ:
        die("Environment variable 'OPENAI_API_KEY' not found")
    connector_name = "openai_connector"
    connector_body = {
        "name": "OpenAI Chat Connector",
        "description": "The connector to public OpenAI model service for GPT 3.5",
        "version": 2,
        "protocol": "http",
        "parameters": {"endpoint": "api.openai.com", "model": "gpt-3.5-turbo", "temperature": 0},
        "credential": {"openAI_key": os.environ["OPENAI_API_KEY"]},
        "actions": [
            {
                "action_type": "predict",
                "method": "POST",
                "url": "https://${parameters.endpoint}/v1/chat/completions",
                "headers": {"Authorization": "Bearer ${credential.openAI_key}"},
                "request_body": '{ "model":"${parameters.model}", \
"messages":${parameters.messages}, "temperature":${parameters.temperature} }',
            }
        ],
    }
    connector_id = create_connector(connector_body, connector_name)
    model_name = "openai"
    openai_register_body = {
        "name": "openAI-gpt-3.5-turbo",
        "function_name": "remote",
        "description": "gpt model",
        "connector_id": connector_id,
        "model_group_id": model_group_id,
    }
    return setup_model(openai_register_body, model_name)


def setup_models():
    """
    Set up all models required in opensearch for the conversation stack
    """
    model_group_id = register_model_group()
    embedding_id = setup_embedding_model(model_group_id)
    reranking_id = setup_reranking_model(model_group_id)
    openai_id = setup_openai_model(model_group_id)
    logging.info(f"ARYN MODEL GROUP ID: {model_group_id}")
    logging.info(f">EMBEDDING MODEL ID: {embedding_id}")
    logging.info(f">RERANKING MODEL ID: {reranking_id}")
    logging.info(f">OPENAI    MODEL ID: {openai_id}")
    return embedding_id, reranking_id, openai_id


def create_pipeline(pipeline_name, pipeline_def):
    """
    Create a pipeline named `pipeline_name` with definition `pipeline_def`
    """
    response = os_request_to_json(f"/_search/pipeline/{pipeline_name}", pipeline_def, "PUT")
    with open(ARYN_STATUSDIR / f"request.{pipeline_name}", "w") as f:
        json.dump(response, f)
    return response


def create_pipelines(openai_id):
    """
    Create the `hybrid_pipeline` and `hybrid_rag_pipeline`
    """
    hybrid_pipeline_name = "hybrid_pipeline"
    pipeline_def = {
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {"technique": "arithmetic_mean", "parameters": {"weights": [0.111, 0.889]}},
                }
            }
        ]
    }
    create_pipeline(hybrid_pipeline_name, pipeline_def)
    hybrid_rag_pipeline_name = "hybrid_rag_pipeline"
    pipeline_def["response_processors"] = [
        {
            "retrieval_augmented_generation": {
                "tag": "openai_pipeline",
                "description": "Pipeline Using OpenAI Connector",
                "model_id": openai_id,
                "context_field_list": ["text_representation"],
            }
        }
    ]
    create_pipeline(hybrid_rag_pipeline_name, pipeline_def)


def configure_opensearch_for_conversational_search():
    """
    Set up all components necessary for conversational search
    """
    embedding_id, reranking_id, openai_id = setup_models()
    create_pipelines(openai_id)


configure_opensearch_for_conversational_search()
