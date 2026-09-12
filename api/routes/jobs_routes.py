"""Job endpoints: quote → job → schedule → execution → complete (GATE 10),
plus scheduling: required skills/hours → available workers → crew (schedule)."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session, JobModel, QuoteModel, OrgCustomerModel
from core.rbac import require_permission
from core.tenancy import AuthContext, scoped_or_404, audit

router = APIRouter(prefix="/jobs", tags=["jobs"])

JOB_STATUSES = {"draft", "scheduled", "in_progress", "completed", "canceled"}


class JobCreateIn(BaseModel):
    org_customer_id: str
    trade: str = Field(min_length=2, max_length=64)
    title: str = ""
    description: str = ""
    quote_id: str | None = None
    scheduled_date: datetime | None = None
    duration_hours: float | None = Field(default=None, ge=0, le=1000)


class JobUpdateIn(BaseModel):
    status: str | None = None
    scheduled_date: datetime | None = None
    duration_hours: float | None = Field(default=None, ge=0, le=1000)
    title: str | None = None
    description: str | None = None


def _job_out(j: JobModel, customer_name: str | None = None, quote_total: float | None = None) -> dict:
    return {
        "id": j.id, "status": j.status, "trade": j.trade, "title": j.title,
        "description": j.description, "quote_id": j.quote_id,
        "customer_id": j.org_customer_id, "customer_name": customer_name,
        "quote_total": quote_total,
        "scheduled_date": j.scheduled_date.isoformat() if j.scheduled_date else None,
        "duration_hours": j.duration_hours,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
        "created_at": j.created_at.isoformat(),
    }


async def _hydrate(session: AsyncSession, j: JobModel) -> dict:
    customer = await session.get(OrgCustomerModel, j.org_customer_id) if j.org_customer_id else None
    quote = await session.get(QuoteModel, j.quote_id) if j.quote_id else None
    return _job_out(j, customer.name if customer else None, quote.total if quote else None)


@router.get("")
async def list_jobs(status: str | None = None,
                    limit: int = 50, offset: int = 0,
                    ctx: AuthContext = Depends(require_permission("jobs:read")),
                    session: AsyncSession = Depends(get_session)):
    stmt = select(JobModel).where(JobModel.organization_id == ctx.organization_id)
    if status:
        if status not in JOB_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(JOB_STATUSES)}")
        stmt = stmt.where(JobModel.status == status)
    stmt = stmt.order_by(JobModel.created_at.desc()).offset(offset).limit(limit)
    jobs = (await session.execute(stmt)).scalars().all()
    return {"jobs": [await _hydrate(session, j) for j in jobs]}


@router.post("", status_code=201)
async def create_job(body: JobCreateIn,
                     ctx: AuthContext = Depends(require_permission("jobs:write")),
                     session: AsyncSession = Depends(get_session)):
    from core.database import OrganizationModel
    customer = await scoped_or_404(session, OrgCustomerModel, body.org_customer_id,
                                   ctx, "Customer")
    if body.quote_id:
        await scoped_or_404(session, QuoteModel, body.quote_id, ctx, "Quote")
    job = JobModel(
        organization_id=ctx.organization_id,
        org_customer_id=customer.id,
        quote_id=body.quote_id,
        trade=body.trade.strip().lower(),
        title=body.title, description=body.description,
        status="scheduled" if body.scheduled_date else "draft",
        scheduled_date=body.scheduled_date,
        duration_hours=body.duration_hours,
        created_by=ctx.user_id,
    )
    session.add(job)
    org = await session.get(OrganizationModel, ctx.organization_id)
    if body.scheduled_date:
        org.onboarding = list(set(org.onboarding or []) | {"first_job_created"})
    await audit(session, "job.created", ctx=ctx, job_id=job.id, trade=job.trade)
    await session.commit()
    return await _hydrate(session, job)


@router.get("/{job_id}")
async def get_job(job_id: str,
                  ctx: AuthContext = Depends(require_permission("jobs:read")),
                  session: AsyncSession = Depends(get_session)):
    job = await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    return await _hydrate(session, job)


@router.patch("/{job_id}")
async def update_job(job_id: str, body: JobUpdateIn,
                     ctx: AuthContext = Depends(require_permission("jobs:write")),
                     session: AsyncSession = Depends(get_session)):
    job = await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    if body.status is not None:
        if body.status not in JOB_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(JOB_STATUSES)}")
        if body.status == "completed":
            job.completed_at = datetime.utcnow()
        else:
            job.completed_at = None
        job.status = body.status
    if body.scheduled_date is not None:
        job.scheduled_date = body.scheduled_date
        if job.status == "draft":
            job.status = "scheduled"
    if body.duration_hours is not None:
        job.duration_hours = body.duration_hours
    if body.title is not None:
        job.title = body.title
    if body.description is not None:
        job.description = body.description
    await audit(session, "job.updated", ctx=ctx, job_id=job_id, status=body.status)
    await session.commit()
    return await _hydrate(session, job)


# ─── Scheduling: required skills/hours → available workers → schedule ───────

from core.database import (  # noqa: E402
    JobRequirementModel, JobAssignmentModel, SkillRateModel, UserModel,
)


class RequirementIn(BaseModel):
    skill_id: str
    workers_needed: int = Field(default=1, ge=1, le=100)
    hours: float = Field(gt=0, le=1000)


class AssignIn(BaseModel):
    user_id: str
    skill_id: str | None = None


def _job_window(job: JobModel) -> tuple[datetime, datetime] | tuple[None, None]:
    """Start/end datetimes for overlap checks."""
    if not job.scheduled_date:
        return None, None
    start = job.scheduled_date
    end = start + timedelta(hours=job.duration_hours or 4.0)
    return start, end


async def _user_busy(session: AsyncSession, user_id: str,
                     start: datetime, end: datetime,
                     exclude_job_id: str | None = None) -> bool:
    """A worker is busy if they're assigned to another active job whose
    window overlaps [start, end)."""
    stmt = (
        select(JobAssignmentModel.job_id)
        .join(JobModel, JobAssignmentModel.job_id == JobModel.id)
        .where(
            JobAssignmentModel.user_id == user_id,
            JobModel.status.in_(["scheduled", "in_progress"]),
            JobModel.scheduled_date.is_not(None),
        ))
    if exclude_job_id:
        stmt = stmt.where(JobModel.id != exclude_job_id)
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return False
    # Overlap check in Python (SQLite lacks portable interval math)
    jobs = (await session.execute(
        select(JobModel).where(JobModel.id.in_(rows)))).scalars().all()
    for other in jobs:
        o_start, o_end = _job_window(other)
        if o_start and o_end and start < o_end and o_start < end:
            return True
    return False


def _requirement_out(r: JobRequirementModel, skill: SkillRateModel | None) -> dict:
    return {
        "id": r.id, "job_id": r.job_id, "skill_id": r.skill_id,
        "skill_name": skill.name if skill else None,
        "skill_level": skill.level if skill else None,
        "workers_needed": r.workers_needed, "hours": r.hours,
    }


def _assignment_out(a: JobAssignmentModel, user: UserModel | None,
                    skill: SkillRateModel | None) -> dict:
    return {
        "id": a.id, "job_id": a.job_id, "user_id": a.user_id,
        "worker_name": (user.full_name or user.email) if user else None,
        "skill_id": a.skill_id,
        "skill_name": skill.name if skill else None,
    }


@router.post("/{job_id}/requirements", status_code=201)
async def add_requirement(job_id: str, body: RequirementIn,
                          ctx: AuthContext = Depends(require_permission("schedules:write")),
                          session: AsyncSession = Depends(get_session)):
    """Declare what a job needs: a skill, how many workers, how many hours."""
    job = await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    skill = await scoped_or_404(session, SkillRateModel, body.skill_id, ctx, "Skill")
    req = JobRequirementModel(organization_id=ctx.organization_id, job_id=job.id,
                              skill_id=skill.id, workers_needed=body.workers_needed,
                              hours=body.hours)
    session.add(req)
    await audit(session, "job.requirement_added", ctx=ctx, job_id=job_id,
                skill=skill.name, workers=body.workers_needed, hours=body.hours)
    await session.commit()
    return _requirement_out(req, skill)


@router.get("/{job_id}/requirements")
async def list_requirements(job_id: str,
                            ctx: AuthContext = Depends(require_permission("schedules:read")),
                            session: AsyncSession = Depends(get_session)):
    await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    reqs = (await session.execute(
        select(JobRequirementModel).where(
            JobRequirementModel.organization_id == ctx.organization_id,
            JobRequirementModel.job_id == job_id))).scalars().all()
    out = []
    for r in reqs:
        skill = await session.get(SkillRateModel, r.skill_id)
        out.append(_requirement_out(r, skill))
    return {"requirements": out}


@router.get("/{job_id}/suggestions")
async def scheduling_suggestions(job_id: str,
                                 ctx: AuthContext = Depends(require_permission("schedules:read")),
                                 session: AsyncSession = Depends(get_session)):
    """For each requirement, which org workers are available in the job's
    time window (skill match noted; availability is the hard constraint)."""
    job = await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    start, end = _job_window(job)
    if not start:
        return {"suggestions": [], "note": "Schedule a date on the job first"}

    reqs = (await session.execute(
        select(JobRequirementModel).where(
            JobRequirementModel.organization_id == ctx.organization_id,
            JobRequirementModel.job_id == job_id))).scalars().all()
    workers = (await session.execute(
        select(UserModel).where(
            UserModel.organization_id == ctx.organization_id,
            UserModel.is_active.is_(True)))).scalars().all()

    busy = {}
    for w in workers:
        busy[w.id] = await _user_busy(session, w.id, start, end, exclude_job_id=job.id)

    suggestions = []
    for r in reqs:
        skill = await session.get(SkillRateModel, r.skill_id)
        matches = [{"user_id": w.id, "name": w.full_name or w.email, "role": w.role}
                   for w in workers if not busy[w.id]]
        suggestions.append({
            **_requirement_out(r, skill),
            "available_workers": matches,
            "already_assigned": len([a for a in (
                await session.execute(
                    select(JobAssignmentModel).where(
                        JobAssignmentModel.job_id == job_id,
                        JobAssignmentModel.skill_id == r.skill_id))).scalars().all()]),
        })
    return {"suggestions": suggestions}


@router.post("/{job_id}/assign", status_code=201)
async def assign_worker(job_id: str, body: AssignIn,
                        ctx: AuthContext = Depends(require_permission("schedules:write")),
                        session: AsyncSession = Depends(get_session)):
    job = await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    worker = await scoped_or_404(session, UserModel, body.user_id, ctx, "User")
    if worker.organization_id != ctx.organization_id or not worker.is_active:
        raise HTTPException(status_code=404, detail="User not found")
    if body.skill_id:
        await scoped_or_404(session, SkillRateModel, body.skill_id, ctx, "Skill")

    start, end = _job_window(job)
    if start and await _user_busy(session, worker.id, start, end, exclude_job_id=job.id):
        raise HTTPException(status_code=409, detail={
            "message": "Worker has an overlapping scheduled job in this window",
            "user_id": worker.id,
        })

    assignment = JobAssignmentModel(organization_id=ctx.organization_id,
                                    job_id=job.id, user_id=worker.id,
                                    skill_id=body.skill_id)
    session.add(assignment)
    if job.status == "draft":
        job.status = "scheduled"
    await audit(session, "job.worker_assigned", ctx=ctx, job_id=job_id,
                user_id=worker.id, skill_id=body.skill_id)
    await session.commit()
    skill = await session.get(SkillRateModel, body.skill_id) if body.skill_id else None
    return _assignment_out(assignment, worker, skill)


@router.delete("/{job_id}/assign/{assignment_id}", status_code=204)
async def unassign_worker(job_id: str, assignment_id: str,
                          ctx: AuthContext = Depends(require_permission("schedules:write")),
                          session: AsyncSession = Depends(get_session)):
    await scoped_or_404(session, JobModel, job_id, ctx, "Job")
    assignment = await scoped_or_404(session, JobAssignmentModel, assignment_id, ctx, "Assignment")
    await session.delete(assignment)
    await audit(session, "job.worker_unassigned", ctx=ctx, job_id=job_id,
                assignment_id=assignment_id)
    await session.commit()


@router.get("/schedule/day")
async def day_schedule(date: str,
                       ctx: AuthContext = Depends(require_permission("schedules:read")),
                       session: AsyncSession = Depends(get_session)):
    """One day's schedule: jobs, their time windows, and assigned workers."""
    try:
        day = datetime.fromisoformat(date).date()
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be ISO format (YYYY-MM-DD)")
    day_start = datetime(day.year, day.month, day.day)
    day_end = day_start + timedelta(days=1)

    jobs = (await session.execute(
        select(JobModel).where(
            JobModel.organization_id == ctx.organization_id,
            JobModel.status.in_(["scheduled", "in_progress"]),
            JobModel.scheduled_date >= day_start,
            JobModel.scheduled_date < day_end,
        ).order_by(JobModel.scheduled_date))).scalars().all()

    out = []
    for job in jobs:
        assignments = (await session.execute(
            select(JobAssignmentModel).where(JobAssignmentModel.job_id == job.id))).scalars().all()
        crew = []
        for a in assignments:
            user = await session.get(UserModel, a.user_id)
            skill = await session.get(SkillRateModel, a.skill_id) if a.skill_id else None
            crew.append(_assignment_out(a, user, skill))
        out.append({**(await _hydrate(session, job)), "crew": crew})
    return {"date": date, "jobs": out}
