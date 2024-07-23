from typing import BinaryIO, Optional
from collections.abc import Mapping
from arynsdk.config import ArynConfig
import requests
import json
import logging


# URL for Aryn Partitioning Service (APS)
APS_URL = "https://api.aryn.cloud/v1/document/partition"


def partition_file(
    file: BinaryIO,
    aryn_api_key: Optional[str] = None,
    aryn_config: ArynConfig = ArynConfig(),
    tables_to_pandas: bool = True,
    threshold: Optional[float] = None,
    use_ocr: bool = False,
    extract_table_structure: bool = False,
    extract_images: bool = False,
    aps_url: str = APS_URL,
) -> dict:
    """
    Sends file to the Aryn Partitioning Service and returns a dict of its document structure and text

    Args:
        file:               open pdf file to partition
        aryn_api_key:       aryn api key, provided as a string
        aryn_config:        ArynConfig object, used for finding an api key. If aryn_api_key is set it will override this.
            default: The default ArynConfig looks in the env var ARYN_API_KEY and the file ~/.aryn/config.yaml
        tables_to_pandas:   convert tables to pandas DataFrame representation.
            default: True
        threshold:  value in [0.0 .. 1.0] to specify the cutoff for detecting bounding boxes.
            default: None (APS will choose)
        use_ocr:    extract text using an OCR model instead of extracting embedded text in PDF.
            default: False
        extract_table_structure: extract tables and their structural content.
            default: False
        extract_images: extract image contents.
            default: False
        aps_url: url of the Aryn Partitioning Service endpoint.
            default: "https://api.aryn.cloud/v1/document/partition"

    Returns:
        A dictionary containing "status" and "elements"

    Example:
         .. code-block:: python

            from arynsdk.partition import partition_file

            with open("my-favorite-pdf.pdf", "rb") as f:
                data = partition_file(
                    f,
                    "MY-ARYN-TOKEN",
                    tables_to_pandas=True,
                    use_ocr=True,
                    extract_table_structure=True,
                    extract_images=True
                )
            elements = data['elements']
    """
    if aryn_api_key is not None:
        if aryn_config is not None:
            logging.warn("Both aryn_api_key and aryn_config were provided. Using aryn_api_key")
        aryn_config = ArynConfig(aryn_api_key=aryn_api_key)

    options_str = _json_options(
        threshold=threshold,
        use_ocr=use_ocr,
        extract_table_structure=extract_table_structure,
        extract_images=extract_images,
    )

    logging.debug(f"{options_str}")

    files: Mapping = {"options": options_str.encode("utf-8"), "pdf": file}

    http_header = {"Authorization": "Bearer {}".format(aryn_config.api_key())}

    resp = requests.post(aps_url, files=files, headers=http_header)

    if resp.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error: status_code: {resp.status_code}, reason: {resp.text}", response=resp
        )

    if tables_to_pandas:
        return _tables_to_pandas(resp.json())
    else:
        return resp.json()


def _json_options(
    threshold: Optional[float] = None,
    use_ocr: bool = False,
    extract_table_structure: bool = False,
    extract_images: bool = False,
) -> str:
    options = dict()
    if threshold:
        options["threshold"] = threshold
    if use_ocr:
        options["use_ocr"] = use_ocr
    if extract_images:
        options["extract_images"] = extract_images
    if extract_table_structure:
        options["extract_table_structure"] = extract_table_structure
    return json.dumps(options)


# Heavily adapted from lib/sycamore/data/table.py::Table.to_csv()
def _tables_to_pandas(data: dict) -> dict:
    import pandas as pd
    import numpy as np
    from collections import OrderedDict

    for e in data["elements"]:
        if e["type"] == "table" and e["table"] is not None:
            table = e["table"]
            header_rows = sorted(
                set(row_num for cell in table["cells"] for row_num in cell["rows"] if cell["is_header"])
            )
            i = -1
            for i in range(len(header_rows)):
                if header_rows[i] != i:
                    break
            max_header_prefix_row = i
            grid_width = table["num_cols"]
            grid_height = table["num_rows"]

            grid = np.empty([grid_height, grid_width], dtype="object")
            for cell in table["cells"]:
                if cell["is_header"] and cell["rows"][0] <= max_header_prefix_row:
                    for col in cell["cols"]:
                        grid[cell["rows"][0], col] = cell["content"]
                    for row in cell["rows"][1:]:
                        for col in cell["cols"]:
                            grid[row, col] = ""
                else:
                    grid[cell["rows"][0], cell["cols"][0]] = cell["content"]
                    for col in cell["cols"][1:]:
                        grid[cell["rows"][0], col] = ""
                    for row in cell["rows"][1:]:
                        for col in cell["cols"]:
                            grid[row, col] = ""

            header = grid[: max_header_prefix_row + 1, :]
            flattened_header = []
            for npcol in header.transpose():
                flattened_header.append(" | ".join(OrderedDict.fromkeys((c for c in npcol if c != ""))))
            df = pd.DataFrame(
                grid[max_header_prefix_row + 1 :, :],
                index=None,
                columns=flattened_header if max_header_prefix_row >= 0 else None,
            )
            e["dataframe"] = df

    return data


def add_bbox_to_pdf():
    raise NotImplementedError("Function add_bbox_to_pdf is not implemented")
