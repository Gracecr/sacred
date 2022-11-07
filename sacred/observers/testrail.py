#!/usr/bin/env python
# coding=utf-8

from datetime import datetime
from typing import Callable

from sacred.observers.base import RunObserver


class TestRailApiObserver(RunObserver):
    VERSION = "TestRailApi-7.5.3"

    def __init__(
        self,
        project_id: int,
        case_id: int,
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
            Project ID
        case_id : int
            Case ID (must be created in TestRail before use)
        run_id : int, optional
            Run ID if continuing an existing run, by default None
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
