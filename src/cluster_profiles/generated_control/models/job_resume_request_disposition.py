from typing import Literal, cast

JobResumeRequestDisposition = Literal['resume', 'retire']

JOB_RESUME_REQUEST_DISPOSITION_VALUES: set[JobResumeRequestDisposition] = { 'resume', 'retire',  }

def check_job_resume_request_disposition(value: str) -> JobResumeRequestDisposition:
    if value in JOB_RESUME_REQUEST_DISPOSITION_VALUES:
        return cast(JobResumeRequestDisposition, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {JOB_RESUME_REQUEST_DISPOSITION_VALUES!r}")
