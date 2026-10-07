import sqlite3
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, text
from sqlalchemy.engine import Engine


db = SQLAlchemy()


def utc_now():

    return datetime.now(timezone.utc)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):

    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    user_id = db.Column(db.Integer, primary_key=True)
    username = db.Column(
        db.String(20),
        nullable=False,
        unique=True,
    )
    email = db.Column(
        db.String(320),
        nullable=False,
        unique=True,
    )
    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )
    timezone = db.Column(
        db.String(64),
        nullable=False,
        default="Asia/Seoul",
        server_default="Asia/Seoul",
    )
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    user_medicines = db.relationship(
        "UserMedicine",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    medication_plans = db.relationship(
        "MedicationPlan",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def get_id(self):

        return str(self.user_id)


class MedicationPlan(db.Model):
    __tablename__ = "medication_plans"

    plan_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "users.user_id",
            ondelete="CASCADE",
            name="fk_medication_plan_user",
        ),
        nullable=False,
        index=True,
    )
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    __table_args__ = (
        db.Index(
            "uq_active_medication_plan_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )

    user = db.relationship(
        "User",
        back_populates="medication_plans",
    )
    times = db.relationship(
        "MedicationPlanTime",
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    schedules = db.relationship(
        "MedicationSchedule",
        back_populates="plan",
        passive_deletes=True,
    )


class MedicationPlanTime(db.Model):
    __tablename__ = "medication_plan_times"

    plan_time_id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "medication_plans.plan_id",
            ondelete="CASCADE",
            name="fk_medication_plan_time_plan",
        ),
        nullable=False,
        index=True,
    )
    time_of_day = db.Column(
        db.Time,
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "plan_id",
            "time_of_day",
            name="uq_medication_plan_time",
        ),
        db.UniqueConstraint(
            "plan_id",
            "plan_time_id",
            name="uq_medication_plan_time_parent_key",
        ),
    )

    plan = db.relationship(
        "MedicationPlan",
        back_populates="times",
    )
    schedule_links = db.relationship(
        "MedicationSchedulePlanTime",
        back_populates="plan_time",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="schedule,plan_time_links",
    )


class PendingRegistration(db.Model):
    __tablename__ = "pending_registrations"

    pending_registration_id = db.Column(
        db.Integer,
        primary_key=True,
    )
    username = db.Column(
        db.String(20),
        nullable=False,
        unique=True,
    )
    email = db.Column(
        db.String(320),
        nullable=False,
        unique=True,
    )
    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )
    verification_code_hash = db.Column(
        db.String(64),
        nullable=False,
    )
    verification_token_hash = db.Column(
        db.String(64),
        nullable=False,
        unique=True,
    )
    expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
    )
    attempt_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    resend_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_sent_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    __table_args__ = (
        db.CheckConstraint(
            "attempt_count BETWEEN 0 AND 5",
            name="ck_pending_registration_attempt_count",
        ),
        db.CheckConstraint(
            "resend_count BETWEEN 0 AND 4",
            name="ck_pending_registration_resend_count",
        ),
    )


class Medicine(db.Model):
    __tablename__ = "medicines"

    item_seq = db.Column(
        db.String(50),
        primary_key=True,
    )
    item_name = db.Column(
        db.String(500),
        nullable=False,
    )
    entp_name = db.Column(
        db.String(255),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    cached_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    user_medicines = db.relationship(
        "UserMedicine",
        back_populates="medicine",
        passive_deletes=True,
    )


class UserMedicine(db.Model):
    __tablename__ = "user_medicines"

    user_medicine_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "users.user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    medicine_item_seq = db.Column(
        db.String(50),
        db.ForeignKey(
            "medicines.item_seq",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    registration_source = db.Column(
        db.String(32),
        nullable=False,
    )
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    registered_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "medicine_item_seq",
            name="uq_user_medicine",
        ),
        db.CheckConstraint(
            "registration_source IN ('search', 'image_recognition')",
            name="ck_user_medicine_registration_source",
        ),
    )

    user = db.relationship(
        "User",
        back_populates="user_medicines",
    )
    medicine = db.relationship(
        "Medicine",
        back_populates="user_medicines",
    )
    schedules = db.relationship(
        "MedicationSchedule",
        back_populates="user_medicine",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MedicationSchedule(db.Model):
    __tablename__ = "medication_schedules"

    schedule_id = db.Column(db.Integer, primary_key=True)
    user_medicine_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "user_medicines.user_medicine_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    plan_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "medication_plans.plan_id",
        ),
        nullable=True,
        index=True,
    )
    supersedes_schedule_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "medication_schedules.schedule_id",
        ),
        nullable=True,
    )
    intake_timing = db.Column(
        db.String(50),
        nullable=False,
    )
    dose_amount_text = db.Column(
        db.String(100),
        nullable=False,
    )
    dose_unit_text = db.Column(
        db.String(100),
        nullable=False,
    )
    instructions = db.Column(
        db.Text,
        nullable=True,
    )
    start_date = db.Column(
        db.Date,
        nullable=False,
    )
    end_date = db.Column(
        db.Date,
        nullable=True,
    )
    course_days = db.Column(
        db.Integer,
        nullable=False,
    )
    reported_doses_taken_before_tracking = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    reminder_tracking_started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
    )
    accounted_occurrence_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    monday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    tuesday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    wednesday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    thursday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    friday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    saturday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    sunday = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    closed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    __table_args__ = (
        db.CheckConstraint(
            "intake_timing IN "
            "('before_meal', 'after_meal', 'regardless_of_meal')",
            name="ck_medication_schedule_intake_timing",
        ),
        db.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_medication_schedule_date_range",
        ),
        db.CheckConstraint(
            "course_days >= 1",
            name="ck_medication_schedule_course_days",
        ),
        db.CheckConstraint(
            "reported_doses_taken_before_tracking >= 0",
            name="ck_medication_schedule_reported_doses",
        ),
        db.CheckConstraint(
            "accounted_occurrence_count >= 0",
            name="ck_medication_schedule_accounted_occurrences",
        ),
        db.CheckConstraint(
            "monday OR tuesday OR wednesday OR thursday "
            "OR friday OR saturday OR sunday",
            name="ck_medication_schedule_weekday",
        ),
        db.Index(
            "uq_active_medication_schedule_per_user_medicine",
            "user_medicine_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        db.Index(
            "uq_medication_schedule_plan_parent_key",
            "plan_id",
            "schedule_id",
            unique=True,
        ),
        db.Index(
            "uq_medication_schedule_supersedes",
            "supersedes_schedule_id",
            unique=True,
        ),
    )

    user_medicine = db.relationship(
        "UserMedicine",
        back_populates="schedules",
    )
    plan = db.relationship(
        "MedicationPlan",
        back_populates="schedules",
    )
    supersedes_schedule = db.relationship(
        "MedicationSchedule",
        remote_side=[schedule_id],
        foreign_keys=[supersedes_schedule_id],
        back_populates="superseded_by_schedule",
    )
    superseded_by_schedule = db.relationship(
        "MedicationSchedule",
        foreign_keys=[supersedes_schedule_id],
        back_populates="supersedes_schedule",
        uselist=False,
    )
    times = db.relationship(
        "MedicationTime",
        back_populates="schedule",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    plan_time_links = db.relationship(
        "MedicationSchedulePlanTime",
        back_populates="schedule",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="plan_time,schedule_links",
    )


class MedicationTime(db.Model):
    __tablename__ = "medication_times"

    medication_time_id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "medication_schedules.schedule_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    time_of_day = db.Column(
        db.Time,
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "schedule_id",
            "time_of_day",
            name="uq_medication_schedule_time",
        ),
    )

    schedule = db.relationship(
        "MedicationSchedule",
        back_populates="times",
    )


class MedicationSchedulePlanTime(db.Model):
    __tablename__ = "medication_schedule_plan_times"

    plan_id = db.Column(
        db.Integer,
        nullable=False,
    )
    schedule_id = db.Column(
        db.Integer,
        nullable=False,
    )
    plan_time_id = db.Column(
        db.Integer,
        nullable=False,
    )

    __table_args__ = (
        db.PrimaryKeyConstraint(
            "schedule_id",
            "plan_time_id",
            name="pk_medication_schedule_plan_time",
        ),
        db.ForeignKeyConstraint(
            ["plan_id", "schedule_id"],
            [
                "medication_schedules.plan_id",
                "medication_schedules.schedule_id",
            ],
            name="fk_schedule_plan_time_schedule",
            ondelete="CASCADE",
        ),
        db.ForeignKeyConstraint(
            ["plan_id", "plan_time_id"],
            [
                "medication_plan_times.plan_id",
                "medication_plan_times.plan_time_id",
            ],
            name="fk_schedule_plan_time_plan_time",
            ondelete="CASCADE",
        ),
    )

    schedule = db.relationship(
        "MedicationSchedule",
        back_populates="plan_time_links",
        foreign_keys=[plan_id, schedule_id],
        overlaps="plan_time,schedule_links",
    )
    plan_time = db.relationship(
        "MedicationPlanTime",
        back_populates="schedule_links",
        foreign_keys=[plan_id, plan_time_id],
        overlaps="schedule,plan_time_links",
    )
