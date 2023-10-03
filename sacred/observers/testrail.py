#!/usr/bin/env python
# coding=utf-8
from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Callable

from sacred import optional as opt
from sacred.commandline_options import cli_option
from sacred.observers.file_storage import FileStorageObserver


class TestRailApiObserver(FileStorageObserver):
    VERSION = "TestRailApi-7.5.3"

    def __init__(
        self,
        project_id: int,
        case_id: int = None,
        section_id: int = None,
        run_id: int = None,
        result_field_hook: Callable[[], dict] = None,
        store_files: bool = False,
        template_id: int = None,
        *args,
        **kwargs,
    ) -> None:
        """Initialize a TestRailApiObserver.

        See `testrail_api.TestRailAPI` for details on extra arguments to pass. It is
        *highly* recommended to use the supported environment variables for your
        TestRail credentials as they will otherwise be published with your source if
        using `store_files` or other observers.

        ```
        TESTRAIL_URL=https://example.testrail.com/
        TESTRAIL_EMAIL=example@mail.com
        TESTRAIL_PASSWORD=password
        ```

        Parameters
        ----------
        project_id : int
            Must be created in TestRail before use.
        case_id : int, optional
            If not included, the case will be determined using the experiment name.
            If no case matching the experiment name exists, one will be created. By
            default None.
        section_id : int, optional
            Only used when `case_id` is `None`, and no case matching the experiment
            name exists. If not defined, a "Sacred" section will be created if a new
            case must be added. By default None.
        run_id : int, optional
            Use when continuing an existing run, by default None.
        result_field_hook : Callable[[], dict], optional
            Extra fields to include in result, by default None
        store_files : bool, optional
            True to store attachments, resources, and sources in TestRail,
            by default False.
        template_id : int, optional
            Template ID to use when creating new cases.
        """
        super().__init__("", copy_artifacts=False, copy_sources=False)
        from testrail_api import TestRailAPI

        self.api = TestRailAPI(*args, **kwargs)
        self.user_id = self.api.users.get_current_user(0)["id"]
        self.project_id = project_id
        self.run_id = run_id
        self.case_id = case_id
        self.section_id = section_id
        if result_field_hook:
            self.result_field_hook = result_field_hook
        else:
            self.result_field_hook = lambda: {}
        self.store_files = store_files
        self.template_id = template_id
        self.start_time: datetime = datetime.now()
        self.attachments: list[str] = []
        self.raw_attachments: dict[str, bytes] = {
            "cout.txt": bytes(),
            "metrics.json": bytes(),
        }
        self.saved_metrics = {}

    def __get_or_create_run(self) -> dict:
        from testrail_api import StatusCodeError, TestRailAPI

        self.api: TestRailAPI
        if self.run_id:
            try:
                print(f"Getting run: {self.run_id}...")
                return self.api.runs.get_run(self.run_id)
            except StatusCodeError as exc:
                raise Exception(
                    f"TestRail Run ID {self.run_id} does not exist."
                ) from exc
            except Exception:
                # This command seems to fail often -- retry
                print(f"Getting run {self.run_id} failed, retrying.")
                self.api = TestRailAPI()
                return self.api.runs.get_run(self.run_id)
        else:
            return self.api.runs.add_run(self.project_id)

    def __get_or_create_case(self, name: str = "") -> dict:
        from testrail_api import StatusCodeError, TestRailAPI

        self.api: TestRailAPI
        if self.case_id:
            try:
                return self.api.cases.get_case(self.case_id)
            except StatusCodeError as exc:
                raise Exception(
                    f"TestRail Case ID {self.case_id} does not exist."
                ) from exc
        else:
            cases = self.api.cases.get_cases(self.project_id, filter=name)["cases"]
            print(f"Cases: {cases}")
            for case in cases:
                if case["title"] == name:
                    return case
            section_id = self.__get_or_create_section()
            if self.template_id is not None:
                return self.api.cases.add_case(
                    section_id, name, template_id=self.template_id
                )
            return self.api.cases.add_case(section_id, name)

    def __get_or_create_section(self) -> int:
        from testrail_api import TestRailAPI

        if self.section_id:
            return self.section_id

        self.api: TestRailAPI
        for section in self.api.sections.get_sections(self.project_id)["sections"]:
            if section["name"] == "Sacred":
                return section
        return self.api.sections.add_section(
            self.project_id,
            "Sacred",
            description="Automatically added by the Sacred TestRail Observer",
        )["id"]

    def _make_run_dir(self, _id):
        # Override FileStoreObserver so that no directory is made
        self.dir = "tmp"

    def save_json(self, obj: dict, filename):
        # Remove "__annotations__" as it's not useful and it is not serializable
        obj_copy = obj.copy()
        clean_dict(obj_copy, lambda key: key == "__annotations__")

        self.raw_attachments[filename] = json.dumps(
            obj,
            indent=2,
            default=lambda obj: obj.to_json()
            if hasattr(obj, "to_json")
            else "<serialization_error>",
        ).encode()

    def save_file(self, filename, target_name=None):
        if target_name:
            raise Exception(
                "TestRailApiObserver.save_file does not support target_name"
            )
        self.attachments.append(filename)

    def save_cout(self):
        self.raw_attachments["cout.txt"] += self.cout[self.cout_write_cursor :].encode()
        self.cout_write_cursor = len(self.cout)

    def render_template(self):
        if opt.has_mako and self.template:
            from mako.template import Template

            template = Template(filename=self.template, output_encoding="utf-8")
            report = template.render(
                run=self.run_entry,
                config=self.config,
                info=self.info,
                cout=self.cout,
                savedir=self.dir,
            )
            assert isinstance(report, bytes)
            ext = self.template.suffix
            self.raw_attachments["report" + ext] = report

    def started_event(
        self, ex_info, command, host_info, start_time, config, meta_info, _id
    ):
        super().started_event(
            ex_info, command, host_info, start_time, config, meta_info, _id
        )
        self.start_time = start_time
        case = self.__get_or_create_case(ex_info["name"])
        self.case_id = case["id"]

    def __upload_result(self, status_id: int, end_time: datetime):
        from testrail_api._session import METHODS

        elapsed = (end_time - self.start_time).seconds
        if elapsed == 0:
            elapsed = 1  # TestRail will not accept 0s elapsed time
        result = self.api.results.add_result_for_case(
            self.__get_or_create_run()["id"],
            self.case_id,
            status_id=status_id,
            elapsed=f"{elapsed}s",
            assignedto_id=self.user_id,
            **self.result_field_hook(),
        )
        if self.store_files:
            for filename in self.attachments:
                self.api.attachments.add_attachment_to_result(result["id"], filename)
            for filename in self.raw_attachments:
                self.api.attachments._session.request(
                    METHODS.POST,
                    f"add_attachment_to_result/{result['id']}",
                    files={
                        "attachment": (
                            filename,
                            io.BytesIO(self.raw_attachments[filename]),
                        )
                    },
                )

    def completed_event(self, stop_time: datetime, result):
        super().completed_event(stop_time, result)
        self.__upload_result(1 if bool(result) else 5, stop_time)

    def interrupted_event(self, interrupt_time: datetime, status):
        super().interrupted_event(interrupt_time, status)
        self.__upload_result(5, interrupt_time)

    def failed_event(self, fail_time: datetime, fail_trace):
        super().failed_event(fail_time, fail_trace)
        self.__upload_result(5, fail_time)

    def resource_event(self, filename: str):
        self.attachments.append(filename)

    def artifact_event(
        self, name: str, filename: str, metadata=None, content_type=None
    ):
        self.attachments.append(filename)

    def log_metrics(self, metrics_by_name, info):
        for metric_name, metric_ptr in metrics_by_name.items():
            if metric_name not in self.saved_metrics:
                self.saved_metrics[metric_name] = metric_ptr.copy()
                timestamps_norm = [ts.isoformat() for ts in metric_ptr["timestamps"]]
                self.saved_metrics[metric_name]["timestamps"] = timestamps_norm
            else:
                self.saved_metrics[metric_name]["values"] += metric_ptr["values"]
                self.saved_metrics[metric_name]["steps"] += metric_ptr["steps"]

                # Manually convert them to avoid passing a datetime dtype handler
                # when we're trying to convert into json.
                timestamps_norm = [ts.isoformat() for ts in metric_ptr["timestamps"]]
                self.saved_metrics[metric_name]["timestamps"] += timestamps_norm
                self.saved_metrics[metric_name]["units"] = metric_ptr["units"]
                self.saved_metrics[metric_name]["depends_on"] = metric_ptr["depends_on"]

        self.save_json(self.saved_metrics, "metrics.json")


@cli_option("-r", "--testrail")
def testrail_option(args, run):
    """Add a TestRail Observer to the experiment.

    The argument value is the project ID, Case ID, and Run ID.

    Project ID is required.

    If Case ID is not provided, the TestRail observer will look for a case with a title
    matching your experiment. If more than 1 case are found, an Exception willl be
    thrown. If no case is found and section ID is defined, a case will be created.
    If no case is found and section ID is NOT defined, an exception will be thrown.

    If Run ID is not provided, a new run will be created.

    Format:
        `project_id=[project_id],case_id=[case_id],[section_id=[section_id]],run_id=[run_id]`
    """
    kwargs = parse_testrail_arg(args)
    mongo = TestRailApiObserver(**kwargs)
    run.observers.append(mongo)


def parse_testrail_arg(arg: str) -> tuple[int, int, int]:
    fields = arg.split(",")
    kwargs = {}
    for field in fields:
        name, value = field.split("=")
        kwargs[name] = int(value)

    if "project_id" not in kwargs is None:
        raise ValueError("testrail argument project_id must be defined.")
    return kwargs


# https://stackoverflow.com/a/60863629
def clean_dict(obj, func):
    """
    Scrolls the entire 'obj' to delete every key for which the 'callable' returns True.

    :param obj: a dictionary or a list of dictionaries to clean
    :param func: a callable that takes a key in argument and return True for each key to delete
    """
    if isinstance(obj, dict):
        # the call to `list` is useless for py2 but makes
        # the code py2/py3 compatible
        for key in list(obj.keys()):
            if func(key):
                del obj[key]
            else:
                clean_dict(obj[key], func)
    elif isinstance(obj, list):
        for i in reversed(range(len(obj))):
            if func(obj[i]):
                del obj[i]
            else:
                clean_dict(obj[i], func)

    else:
        # neither a dict nor a list, do nothing
        pass
