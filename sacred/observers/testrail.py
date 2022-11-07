#!/usr/bin/env python
# coding=utf-8
from __future__ import annotations

from datetime import datetime
from typing import Callable

from sacred.commandline_options import cli_option
from sacred.observers.base import RunObserver


class TestRailApiObserver(RunObserver):
    VERSION = "TestRailApi-7.5.3"

    def __init__(
        self,
        project_id: int,
        case_id: int = None,
        section_id: int = None,
        run_id: int = None,
        result_field_hook: Callable[[], dict] = None,
        store_files: bool = False,
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
            If no case matching the experiment name exists, and `section_id` is defined, one will be created for you.
        section_id : int, optional
            Case ID (must be created in TestRail before use)
        run_id : int, optional
            Use when continuing an existing run, by default None.
        result_field_hook : Callable[[], dict], optional
            Extra fields to include in result, by default None
        store_files : bool, optional
            True to store attachments, resources, and sources in TestRail,
            by default False
        """
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
        self.start_time: datetime = None
        self.attachments: list[str] = []

    def __get_or_create_run(self):
        from testrail_api import TestRailAPI, StatusCodeError

        self.api: TestRailAPI
        if self.run_id:
            try:
                return self.api.runs.get_run(self.run_id)
            except StatusCodeError as exc:
                raise Exception(
                    f"TestRail Run ID {self.run_id} does not exist."
                ) from exc
        else:
            response = self.api.runs.add_run(self.project_id)
            return response

    def __get_or_create_case(self, name: str = None):
        from testrail_api import TestRailAPI, StatusCodeError
        from testrail_api._exception import TestRailAPIError

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
            if len(cases) == 1:
                return cases[0]
            if len(cases) > 1:
                print(cases)
                raise TestRailAPIError(f"{len(cases)} cases match {name}, expected 1.")
            if self.section_id:
                return self.api.cases.add_case(self.section_id, name)

    def queued_event(
        self, ex_info, command, host_info, queue_time, config, meta_info, _id
    ):
        pass

    def save_sources(self, ex_info):
        return []  # TODO

    def started_event(
        self, ex_info, command, host_info, start_time, config, meta_info, _id
    ):
        self.start_time = start_time
        case = self.__get_or_create_case(ex_info["name"])
        self.case_id = case["id"]

    def heartbeat_event(self, info, captured_out, beat_time, result):
        pass

    def __upload_result(self, status_id: int, end_time: datetime):
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

    def completed_event(self, stop_time: datetime, result):
        self.__upload_result(1, stop_time)

    def interrupted_event(self, interrupt_time: datetime, status):
        self.__upload_result(5, interrupt_time)

    def failed_event(self, fail_time: datetime, fail_trace):
        self.__upload_result(5, fail_time)

    def resource_event(self, filename: str):
        self.attachments.append(filename)

    def artifact_event(
        self, name: str, filename: str, metadata=None, content_type=None
    ):
        self.attachments.append(filename)

    def log_metrics(self, metrics_by_name, info):
        pass


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
        `project=[project_id],case=[case_id],[section=[section_id]],run=[run_id]`
    """
    kwargs = parse_testrail_arg(args)
    mongo = TestRailApiObserver(**kwargs)
    run.observers.append(mongo)


def parse_testrail_arg(arg: str) -> tuple[int, int, int]:
    fields = arg.split(",")
    kwargs = {}
    for field in fields:
        if field.startswith("project="):
            kwargs["project_id"] = int(field[len("project=")])
        elif field.startswith("case="):
            kwargs["case_id"] = int(field[len("case=")])
        elif field.startswith("run="):
            kwargs["run_id"] = int(field[len("run=")])
        elif field.startswith("section="):
            kwargs["section_id"] = int(field[len("section=")])

    if "project_id" not in kwargs is None:
        raise ValueError("testrail argument project_id must be defined.")
    return kwargs
