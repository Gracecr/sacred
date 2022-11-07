#!/usr/bin/env python
# coding=utf-8


import datetime
import pint

import pytest
from sacred.metrics_logger import ScalarMetricLogEntry, linearize_metrics
from sacred.serializer import json

sqlalchemy = pytest.importorskip("sqlalchemy")

from sacred.observers.sql import SqlObserver
from sacred.observers.sql_bases import (
    Base,
    Host,
    Experiment,
    Run,
    Source,
    Resource,
    Metric,
)


T1 = datetime.datetime(1999, 5, 4, 3, 2, 1, 0)
T2 = datetime.datetime(1999, 5, 5, 5, 5, 5, 5)


@pytest.fixture
def engine(request):
    """Engine configuration."""
    url = request.config.getoption("--sqlalchemy-connect-url")
    from sqlalchemy.engine import create_engine

    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    from sqlalchemy.orm import sessionmaker, scoped_session

    connection = engine.connect()
    trans = connection.begin()
    session_factory = sessionmaker(bind=engine)
    # make session thread-local to avoid problems with sqlite (see #275)
    session = scoped_session(session_factory)
    yield session
    session.close()
    trans.rollback()
    # Postgres does not support nested transactions in the same way SQLite does
    Base.metadata.drop_all(connection)
    connection.close()


@pytest.fixture
def sql_obs(session, engine):
    return SqlObserver.create_from(engine, session)


@pytest.fixture
def sample_run():
    exp = {
        "name": "test_exp",
        "sources": [],
        "repositories": [],
        "dependencies": [],
        "base_dir": "/tmp",
    }
    host = {
        "hostname": "test_host",
        "cpu": "Intel",
        "os": ["Linux", "Ubuntu"],
        "python_version": "3.4",
    }
    config = {"config": "True", "foo": "bar", "answer": 42}
    command = "run"
    meta_info = {"comment": "test run"}
    return {
        "_id": "FEDCBA9876543210",
        "ex_info": exp,
        "command": command,
        "host_info": host,
        "start_time": T1,
        "config": config,
        "meta_info": meta_info,
    }


def test_sql_observer_started_event_creates_run(sql_obs, sample_run, session):
    sample_run["_id"] = None
    _id = sql_obs.started_event(**sample_run)
    assert _id is not None
    assert session.query(Run).count() == 1
    assert session.query(Host).count() == 1
    assert session.query(Experiment).count() == 1
    run: Run = session.query(Run).first()
    assert run.to_json() == {
        "_id": _id,
        "command": sample_run["command"],
        "start_time": sample_run["start_time"],
        "heartbeat": None,
        "stop_time": None,
        "queue_time": None,
        "status": "RUNNING",
        "result": None,
        "meta": {"comment": sample_run["meta_info"]["comment"], "priority": 0.0},
        "resources": [],
        "artifacts": [],
        "metrics": [],
        "host": sample_run["host_info"],
        "experiment": sample_run["ex_info"],
        "config": sample_run["config"],
        "captured_out": None,
        "fail_trace": None,
    }


def test_sql_observer_started_event_uses_given_id(sql_obs, sample_run, session):
    _id = sql_obs.started_event(**sample_run)
    assert _id == sample_run["_id"]
    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()
    assert db_run.run_id == sample_run["_id"]


def test_fs_observer_started_event_saves_source(sql_obs, sample_run, session, tmpfile):
    sample_run["ex_info"]["sources"] = [[tmpfile.name, tmpfile.md5sum]]

    sql_obs.started_event(**sample_run)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()
    assert session.query(Source).count() == 1
    assert len(db_run.experiment.sources) == 1
    source = db_run.experiment.sources[0]
    assert source.filename == tmpfile.name
    assert source.content == "import sacred\n"
    assert source.md5sum == tmpfile.md5sum


def test_sql_observer_heartbeat_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)

    info = {"my_info": [1, 2, 3], "nr": 7}
    outp = "some output"
    sql_obs.heartbeat_event(info=info, captured_out=outp, beat_time=T2, result=23.5)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()
    assert db_run.heartbeat == T2
    assert db_run.result == 23.5
    assert json.decode(db_run.info) == info
    assert db_run.captured_out == outp


def test_sql_observer_completed_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)
    sql_obs.completed_event(stop_time=T2, result=42)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert db_run.stop_time == T2
    assert db_run.result == 42
    assert db_run.status == "COMPLETED"


def test_sql_observer_interrupted_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)
    sql_obs.interrupted_event(interrupt_time=T2, status="INTERRUPTED")

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert db_run.stop_time == T2
    assert db_run.status == "INTERRUPTED"


def test_sql_observer_failed_event_updates_run(sql_obs, sample_run, session):
    sql_obs.started_event(**sample_run)
    fail_trace = ["lots of errors and", "so", "on..."]
    sql_obs.failed_event(fail_time=T2, fail_trace=fail_trace)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert db_run.stop_time == T2
    assert db_run.status == "FAILED"
    assert db_run.fail_trace == "lots of errors and\nso\non..."


def test_sql_observer_artifact_event(sql_obs, sample_run, session, tmpfile):
    sql_obs.started_event(**sample_run)

    sql_obs.artifact_event("my_artifact.py", tmpfile.name)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert len(db_run.artifacts) == 1
    artifact = db_run.artifacts[0]

    assert artifact.filename == "my_artifact.py"
    assert artifact.content.decode() == tmpfile.content


def test_fs_observer_resource_event(sql_obs, sample_run, session, tmpfile):
    sql_obs.started_event(**sample_run)

    sql_obs.resource_event(tmpfile.name)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    assert len(db_run.resources) == 1
    res = db_run.resources[0]
    assert res.filename == tmpfile.name
    assert res.md5sum == tmpfile.md5sum
    assert res.content.decode() == tmpfile.content


def test_fs_observer_doesnt_duplicate_sources(sql_obs, sample_run, session, tmpfile):
    sql_obs2 = SqlObserver.create_from(sql_obs.engine, session)
    sample_run["_id"] = None
    sample_run["ex_info"]["sources"] = [[tmpfile.name, tmpfile.md5sum]]

    sql_obs.started_event(**sample_run)
    sql_obs2.started_event(**sample_run)

    assert session.query(Run).count() == 2
    assert session.query(Source).count() == 1


def test_fs_observer_doesnt_duplicate_resources(sql_obs, sample_run, session, tmpfile):
    sql_obs2 = SqlObserver.create_from(sql_obs.engine, session)
    sample_run["_id"] = None
    sample_run["ex_info"]["sources"] = [[tmpfile.name, tmpfile.md5sum]]

    sql_obs.started_event(**sample_run)
    sql_obs2.started_event(**sample_run)

    sql_obs.resource_event(tmpfile.name)
    sql_obs2.resource_event(tmpfile.name)

    assert session.query(Run).count() == 2
    assert session.query(Resource).count() == 1


def test_sql_observer_equality(sql_obs, engine, session):
    sql_obs2 = SqlObserver.create_from(engine, session)
    assert sql_obs == sql_obs2

    assert not sql_obs != sql_obs2

    assert not sql_obs == "foo"
    assert sql_obs != "foo"


@pytest.fixture
def logged_metrics():
    return [
        ScalarMetricLogEntry("training.loss", 10, datetime.datetime.utcnow(), 1),
        ScalarMetricLogEntry("training.loss", 20, datetime.datetime.utcnow(), 2),
        ScalarMetricLogEntry("training.loss", 30, datetime.datetime.utcnow(), 3),
        ScalarMetricLogEntry("training.accuracy", 10, datetime.datetime.utcnow(), 100),
        ScalarMetricLogEntry("training.accuracy", 20, datetime.datetime.utcnow(), 200),
        ScalarMetricLogEntry("training.accuracy", 30, datetime.datetime.utcnow(), 300),
        ScalarMetricLogEntry("training.loss", 40, datetime.datetime.utcnow(), 10),
        ScalarMetricLogEntry("training.loss", 50, datetime.datetime.utcnow(), 20),
        ScalarMetricLogEntry("training.loss", 60, datetime.datetime.utcnow(), 30),
    ]


def test_log_metrics(sql_obs: SqlObserver, sample_run, logged_metrics, session):
    """
    Test storing scalar measurements

    Test whether measurements logged using _run.metrics.log_scalar_metric
    are being stored in the 'metrics' table
    and that the metrics have a valid reference to the associtaed 'run'.

    Metrics are identified by name (e.g.: 'training.loss') and by the
    experiment run that produced them. Each metric contains a list of x values
    (e.g. iteration step), y values (measured values) and timestamps of when
    each of the measurements was taken.
    """

    # Start the experiment
    sql_obs.started_event(**sample_run)

    # Initialize the info dictionary and standard output with arbitrary values
    info = {"my_info": [1, 2, 3], "nr": 7}
    outp = "some output"

    # Take first 6 measured events, group them by metric name
    # and store the measured series to the 'metrics' collection
    # and reference the newly created records in the 'info' dictionary.
    sql_obs.log_metrics(linearize_metrics(logged_metrics[:6]), info)
    # Call standard heartbeat event (store the info dictionary to the database)
    sql_obs.heartbeat_event(info=info, captured_out=outp, beat_time=T1, result=0)

    # There should be only one run stored
    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    # The metrics, stored in the metrics table,
    # should be two (training.loss and training.accuracy)
    assert len(db_run.metrics) == 2
    loss = next(metric for metric in db_run.metrics if metric.name == "training.loss")
    assert [step.step for step in loss.steps] == [10, 20, 30]
    assert [value.value for value in loss.values] == [1, 2, 3]
    for i in range(len(loss.timestamps) - 1):
        assert loss.timestamps[i].timestamp <= loss.timestamps[i + 1].timestamp

    # Read the training.accuracy metric and check the references as with the training.loss above
    accuracy = next(
        metric for metric in db_run.metrics if metric.name == "training.accuracy"
    )
    assert [step.step for step in accuracy.steps] == [10, 20, 30]
    assert [value.value for value in accuracy.values] == [100, 200, 300]

    # Now, process the remaining events
    # The metrics shouldn't be overwritten, but appended instead.
    sql_obs.log_metrics(linearize_metrics(logged_metrics[6:]), info)
    sql_obs.heartbeat_event(info=info, captured_out=outp, beat_time=T2, result=0)

    assert session.query(Run).count() == 1
    db_run = session.query(Run).first()

    # The newly added metrics belong to the same run and have the same names, so the total number
    # of metrics should not change.
    assert len(db_run.metrics) == 2
    loss = next(metric for metric in db_run.metrics if metric.name == "training.loss")
    # ... but the values should be appended to the original list
    assert [step.step for step in loss.steps] == [10, 20, 30, 40, 50, 60]
    assert [value.value for value in loss.values] == [1, 2, 3, 10, 20, 30]
    for i in range(len(loss.timestamps) - 1):
        assert loss.timestamps[i].timestamp <= loss.timestamps[i + 1].timestamp

    accuracy = next(
        metric for metric in db_run.metrics if metric.name == "training.accuracy"
    )
    assert [step.step for step in accuracy.steps] == [10, 20, 30]
    assert [value.value for value in accuracy.values] == [100, 200, 300]

    # Make sure that when starting a new experiment, new records in metrics are created
    # instead of appending to the old ones.
    sample_run["_id"] = "NEWID"
    # Start the experiment
    sql_obs.started_event(**sample_run)
    sql_obs.log_metrics(linearize_metrics(logged_metrics[:4]), info)
    sql_obs.heartbeat_event(info=info, captured_out=outp, beat_time=T1, result=0)
    # A new run has been created
    assert session.query(Run).count() == 2
    # Another 2 metrics have been created
    assert session.query(Metric).count() == 4

    # Attempt to insert a metric with units
    sql_obs.log_metrics(
        linearize_metrics(
            [
                ScalarMetricLogEntry(
                    "training.units",
                    1,
                    datetime.datetime.utcnow(),
                    pint.Quantity(1, "meter"),
                )
            ]
        ),
        info,
    )
    sql_obs.heartbeat_event(info=info, captured_out=outp, beat_time=T1, result=0)
    assert session.query(Metric).count() == 5
    db_run = session.get(Run, sql_obs.run.id)
    units = next(metric for metric in db_run.metrics if metric.name == "training.units")
    assert units.values[0].value == 1
    assert units.units == "meter"
