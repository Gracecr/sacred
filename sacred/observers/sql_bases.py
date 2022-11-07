from __future__ import annotations

import hashlib
import json
import os

import sqlalchemy as sa
from sqlalchemy.orm import relationship, Session
from sqlalchemy.ext.declarative import declarative_base

from sacred.dependencies import get_digest
from sacred.serializer import restore


Base = declarative_base()


class Source(Base):
    __tablename__ = "source"

    @classmethod
    def get_or_create(cls, filename, md5sum, session, basedir):
        instance = (
            session.query(cls).filter_by(filename=filename, md5sum=md5sum).first()
        )
        if instance:
            return instance
        full_path = os.path.join(basedir, filename)
        md5sum_ = get_digest(full_path)
        assert md5sum_ == md5sum, "found md5 mismatch for {}: {} != {}".format(
            filename, md5sum, md5sum_
        )
        with open(full_path, "r") as f:
            return cls(filename=filename, md5sum=md5sum, content=f.read())

    source_id = sa.Column(sa.Integer, primary_key=True)
    filename = sa.Column(sa.String(256))
    md5sum = sa.Column(sa.String(32))
    content = sa.Column(sa.Text)

    def to_json(self):
        return {"filename": self.filename, "md5sum": self.md5sum}


class Repository(Base):
    __tablename__ = "repository"

    @classmethod
    def get_or_create(cls, url, commit, dirty, session):
        instance = (
            session.query(cls).filter_by(url=url, commit=commit, dirty=dirty).first()
        )
        if instance:
            return instance
        return cls(url=url, commit=commit, dirty=dirty)

    repository_id = sa.Column(sa.Integer, primary_key=True)
    url = sa.Column(sa.String(2048))
    commit = sa.Column(sa.String(40))
    dirty = sa.Column(sa.Boolean)

    def to_json(self):
        return {"url": self.url, "commit": self.commit, "dirty": self.dirty}


class Dependency(Base):
    __tablename__ = "dependency"

    @classmethod
    def get_or_create(cls, dep, session):
        name, _, version = dep.partition("==")
        instance = session.query(cls).filter_by(name=name, version=version).first()
        if instance:
            return instance
        return cls(name=name, version=version)

    dependency_id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(32))
    version = sa.Column(sa.String(16))

    def to_json(self):
        return "{}=={}".format(self.name, self.version)


class Artifact(Base):
    __tablename__ = "artifact"

    @classmethod
    def create(cls, name, filename):
        with open(filename, "rb") as f:
            return cls(filename=name, content=f.read())

    artifact_id = sa.Column(sa.Integer, primary_key=True)
    filename = sa.Column(sa.String(64))
    content = sa.Column(sa.LargeBinary)

    run_id = sa.Column(sa.String(24), sa.ForeignKey("run.run_id"))
    run = relationship("Run", back_populates="artifacts")

    def to_json(self):
        return {"_id": self.artifact_id, "filename": self.filename}


class Resource(Base):
    __tablename__ = "resource"

    @classmethod
    def get_or_create(cls, filename, session):
        md5sum = get_digest(filename)
        instance = (
            session.query(cls).filter_by(filename=filename, md5sum=md5sum).first()
        )
        if instance:
            return instance
        with open(filename, "rb") as f:
            return cls(filename=filename, md5sum=md5sum, content=f.read())

    resource_id = sa.Column(sa.Integer, primary_key=True)
    filename = sa.Column(sa.String(256))
    md5sum = sa.Column(sa.String(32))
    content = sa.Column(sa.LargeBinary)

    def to_json(self):
        return {"filename": self.filename, "md5sum": self.md5sum}


class Host(Base):
    __tablename__ = "host"

    @classmethod
    def get_or_create(cls, host_info, session):
        h = dict(
            hostname=host_info["hostname"],
            cpu=host_info["cpu"],
            os=host_info["os"][0],
            os_info=host_info["os"][1],
            python_version=host_info["python_version"],
        )

        return session.query(cls).filter_by(**h).first() or cls(**h)

    host_id = sa.Column(sa.Integer, primary_key=True)
    cpu = sa.Column(sa.String(64))
    hostname = sa.Column(sa.String(64))
    os = sa.Column(sa.String(16))
    os_info = sa.Column(sa.String(64))
    python_version = sa.Column(sa.String(16))
    runs: list[Run] = relationship("Run", back_populates="host")

    def to_json(self):
        return {
            "cpu": self.cpu,
            "hostname": self.hostname,
            "os": [self.os, self.os_info],
            "python_version": self.python_version,
        }


experiment_source_association = sa.Table(
    "experiments_sources",
    Base.metadata,
    sa.Column("experiment_id", sa.Integer, sa.ForeignKey("experiment.experiment_id")),
    sa.Column("source_id", sa.Integer, sa.ForeignKey("source.source_id")),
)

experiment_repository_association = sa.Table(
    "experiments_repositories",
    Base.metadata,
    sa.Column("experiment_id", sa.Integer, sa.ForeignKey("experiment.experiment_id")),
    sa.Column("repository_id", sa.Integer, sa.ForeignKey("repository.repository_id")),
)

experiment_dependency_association = sa.Table(
    "experiments_dependencies",
    Base.metadata,
    sa.Column("experiment_id", sa.Integer, sa.ForeignKey("experiment.experiment_id")),
    sa.Column("dependency_id", sa.Integer, sa.ForeignKey("dependency.dependency_id")),
)


class Experiment(Base):
    __tablename__ = "experiment"

    @classmethod
    def get_or_create(cls, ex_info, session):
        name = ex_info["name"]
        # Compute a MD5sum of the ex_info to determine its uniqueness
        h = hashlib.md5()
        h.update(json.dumps(ex_info).encode())
        md5 = h.hexdigest()
        instance = session.query(cls).filter_by(name=name, md5sum=md5).first()
        if instance:
            return instance

        dependencies = [
            Dependency.get_or_create(d, session) for d in ex_info["dependencies"]
        ]
        sources = [
            Source.get_or_create(s, md5sum, session, ex_info["base_dir"])
            for s, md5sum in ex_info["sources"]
        ]
        repositories = set()
        for r in ex_info["repositories"]:
            repository = Repository.get_or_create(
                r["url"], r["commit"], r["dirty"], session
            )
            session.add(repository)
            repositories.add(repository)
        repositories = list(repositories)

        return cls(
            name=name,
            dependencies=dependencies,
            sources=sources,
            repositories=repositories,
            md5sum=md5,
            base_dir=ex_info["base_dir"],
        )

    experiment_id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(32))
    md5sum = sa.Column(sa.String(32))
    base_dir = sa.Column(sa.String(64))
    sources = relationship(
        "Source", secondary=experiment_source_association, backref="experiments"
    )
    repositories = relationship(
        "Repository", secondary=experiment_repository_association, backref="experiments"
    )
    dependencies = relationship(
        "Dependency", secondary=experiment_dependency_association, backref="experiments"
    )

    def to_json(self):
        return {
            "name": self.name,
            "base_dir": self.base_dir,
            "sources": [s.to_json() for s in self.sources],
            "repositories": [r.to_json() for r in self.repositories],
            "dependencies": [d.to_json() for d in self.dependencies],
        }


class MetricAssociation(Base):
    __tablename__ = "metric_association"

    dependent_id = sa.Column(
        sa.Integer, sa.ForeignKey("metric.metric_id"), primary_key=True
    )
    independent_id = sa.Column(
        sa.Integer, sa.ForeignKey("metric.metric_id"), primary_key=True
    )


class Metric(Base):
    __tablename__ = "metric"

    @classmethod
    def create(cls, run, metric_name: str, metric_info: dict, session: Session):
        dependencies = list(
            session.execute(
                sa.select(Metric).where(
                    Metric.run == run, Metric.name.in_(metric_info["depends_on"])
                )
            ).scalars()
        )
        print(dependencies)
        metric = Metric(
            run=run,
            name=metric_name,
            units=metric_info["units"],
            depends_on=dependencies,
        )
        metric.steps.extend(
            [
                MetricStep(metric_id=metric.metric_id, step=step)
                for step in metric_info["steps"]
            ]
        )
        metric.values.extend(
            [
                MetricValue(metric_id=metric.metric_id, value=value)
                for value in metric_info["values"]
            ]
        )
        metric.timestamps.extend(
            [
                MetricTimestamp(metric_id=metric.metric_id, timestamp=timestamp)
                for timestamp in metric_info["timestamps"]
            ]
        )

        return metric

    metric_id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String, sa.UniqueConstraint())
    steps: list[MetricStep] = relationship("MetricStep")
    values: list[MetricValue] = relationship("MetricValue")
    timestamps: list[MetricTimestamp] = relationship("MetricTimestamp")
    # Fun fact:
    # British thermal unit (international table) inch per second square-foot degree Fahrenheit
    # is the longest unit name in the UNECE's list at 88 characters
    units = sa.Column(sa.String)
    depends_on: list[Metric] = relationship(
        "Metric",
        secondary="metric_association",
        primaryjoin=metric_id == MetricAssociation.independent_id,
        secondaryjoin=metric_id == MetricAssociation.dependent_id,
    )

    run_id = sa.Column(sa.String(24), sa.ForeignKey("run.run_id"))
    run = relationship("Run", back_populates="metrics")

    def to_json(self):
        return {
            "name": self.name,
            "values": [value.value for value in self.values],
            "steps": [step.step for step in self.steps],
            "timestamps": [d.timestamp.isoformat() for d in self.timestamps],
            "units": self.units,
        }


class MetricValue(Base):
    __tablename__ = "metric_value"
    id = sa.Column(sa.Integer, primary_key=True)
    metric_id = sa.Column(sa.Integer, sa.ForeignKey("metric.metric_id"))
    value = sa.Column(sa.Float)


class MetricStep(Base):
    __tablename__ = "metric_step"
    id = sa.Column(sa.Integer, primary_key=True)
    metric_id = sa.Column(sa.Integer, sa.ForeignKey("metric.metric_id"))
    step = sa.Column(sa.Float)


class MetricTimestamp(Base):
    __tablename__ = "metric_timestamp"
    id = sa.Column(sa.Integer, primary_key=True)
    metric_id = sa.Column(sa.Integer, sa.ForeignKey("metric.metric_id"))
    timestamp = sa.Column(sa.DateTime)


run_resource_association = sa.Table(
    "runs_resources",
    Base.metadata,
    sa.Column("run_id", sa.String(24), sa.ForeignKey("run.run_id")),
    sa.Column("resource_id", sa.Integer, sa.ForeignKey("resource.resource_id")),
)


class Run(Base):
    __tablename__ = "run"
    id = sa.Column(sa.Integer, primary_key=True)

    run_id = sa.Column(sa.String(24), unique=True)

    command = sa.Column(sa.String(64))

    # times
    start_time = sa.Column(sa.DateTime)
    heartbeat = sa.Column(sa.DateTime)
    stop_time = sa.Column(sa.DateTime)
    queue_time = sa.Column(sa.DateTime)

    # meta info
    priority = sa.Column(sa.Float)
    comment = sa.Column(sa.Text)

    fail_trace = sa.Column(sa.Text)

    # Captured out
    # TODO: move to separate table?
    captured_out = sa.Column(sa.Text)

    # Configuration & info
    # TODO: switch type to json if possible
    config = sa.Column(sa.Text)
    info = sa.Column(sa.Text)

    status = sa.Column(
        sa.Enum(
            "RUNNING",
            "COMPLETED",
            "INTERRUPTED",
            "TIMEOUT",
            "FAILED",
            name="status_enum",
        )
    )

    host_id = sa.Column(sa.Integer, sa.ForeignKey("host.host_id"))
    host: Host = relationship("Host", back_populates="runs")

    experiment_id = sa.Column(sa.Integer, sa.ForeignKey("experiment.experiment_id"))
    experiment = relationship("Experiment", backref=sa.orm.backref("runs"))

    artifacts: list[Artifact] = relationship("Artifact", back_populates="run")

    resources: list[Resource] = relationship(
        "Resource", secondary=run_resource_association, backref="runs"
    )
    metrics: list[Metric] = relationship("Metric", back_populates="run")
    result = sa.Column(sa.Float)

    def to_json(self):
        return {
            "_id": self.run_id,
            "command": self.command,
            "start_time": self.start_time,
            "heartbeat": self.heartbeat,
            "stop_time": self.stop_time,
            "queue_time": self.queue_time,
            "status": self.status,
            "result": self.result,
            "meta": {"comment": self.comment, "priority": self.priority},
            "resources": [r.to_json() for r in self.resources],
            "artifacts": [a.to_json() for a in self.artifacts],
            "metrics": [m.to_json() for m in self.metrics],
            "host": self.host.to_json(),
            "experiment": self.experiment.to_json(),
            "config": restore(json.loads(self.config)),
            "captured_out": self.captured_out,
            "fail_trace": self.fail_trace,
        }
