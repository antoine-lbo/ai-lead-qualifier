"""
Tests for the batch processing module.

Tests CSV parsing, concurrent lead processing, job management,
SSE streaming, and export functionality.
"""

import pytest
import asyncio
import json
import csv
import io
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from fastapi import UploadFile
from fastapi.testclient import TestClient

from src.batch import (
    BatchJob,
    BatchSummary,
    JobStatus,
    parse_csv_leads,
    run_batch,
    router as batch_router,
)
from src.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_csv_content():
    """Valid CSV with required and optional columns."""
    return (
        "email,company,message,name,phone\n"
        "alice@acme.com,Acme Corp,Need automation for 200 employees,Alice Smith,555-0101\n"
        "bob@startup.io,StartupIO,Looking for AI scoring,Bob Jones,\n"
        "carol@bigcorp.com,BigCorp Inc,Enterprise deal 500 seats,Carol Lee,555-0303\n"
    )


@pytest.fixture
def minimal_csv_content():
    """CSV with only required columns."""
    return (
        "email,company,message\n"
        "test@example.com,TestCo,Interested in your product\n"
    )


@pytest.fixture
def invalid_csv_missing_columns():
    """CSV missing required columns."""
    return "name,phone\nalice,555-0101\n"


@pytest.fixture
def empty_csv():
    """CSV with headers but no data rows."""
    return "email,company,message\n"


@pytest.fixture
def mock_qualification_result():
    """Standard qualification result returned by the qualifier."""
    return {
        "score": 85,
        "tier": "HOT",
        "reasoning": "Enterprise company with clear budget indicators",
        "recommended_action": "route_to_ae",
        "enrichment": {
            "company_size": "200-500",
            "industry": "Technology",
            "estimated_revenue": "$50M-$100M",
        },
    }


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# CSV Parsing Tests
# ---------------------------------------------------------------------------

class TestCSVParsing:
    """Tests for parse_csv_leads function."""

    @pytest.mark.asyncio
    async def test_parse_valid_csv(self, sample_csv_content):
        """Should parse all rows from a valid CSV."""
        leads = await parse_csv_leads(sample_csv_content)
        assert len(leads) == 3
        assert leads[0]["email"] == "alice@acme.com"
        assert leads[0]["company"] == "Acme Corp"
        assert leads[1]["email"] == "bob@startup.io"
        assert leads[2]["company"] == "BigCorp Inc"

    @pytest.mark.asyncio
    async def test_parse_minimal_csv(self, minimal_csv_content):
        """Should parse CSV with only required columns."""
        leads = await parse_csv_leads(minimal_csv_content)
        assert len(leads) == 1
        assert leads[0]["email"] == "test@example.com"
        assert "name" not in leads[0] or leads[0].get("name") == ""

    @pytest.mark.asyncio
    async def test_parse_csv_missing_required_columns(self, invalid_csv_missing_columns):
        """Should raise ValueError when required columns are missing."""
        with pytest.raises(ValueError, match="Missing required columns"):
            await parse_csv_leads(invalid_csv_missing_columns)

    @pytest.mark.asyncio
    async def test_parse_empty_csv(self, empty_csv):
        """Should raise ValueError for CSV with no data rows."""
        with pytest.raises(ValueError, match="No data rows"):
            await parse_csv_leads(empty_csv)

    @pytest.mark.asyncio
    async def test_parse_csv_strips_whitespace(self):
        """Should strip whitespace from headers and values."""
        content = " email , company , message \n alice@test.com , TestCo , Hello \n"
        leads = await parse_csv_leads(content)
        assert leads[0]["email"] == "alice@test.com"
        assert leads[0]["company"] == "TestCo"

    @pytest.mark.asyncio
    async def test_parse_csv_skips_empty_rows(self):
        """Should skip rows where email is empty."""
        content = "email,company,message\nalice@test.com,TestCo,Hi\n,,\nbob@test.com,BobCo,Hello\n"
        leads = await parse_csv_leads(content)
        assert len(leads) == 2


# ---------------------------------------------------------------------------
# BatchJob Model Tests
# ---------------------------------------------------------------------------

class TestBatchJob:
    """Tests for the BatchJob model and its computed properties."""

    def test_job_creation_defaults(self):
        """New job should have correct default values."""
        job = BatchJob(
            id="test-123",
            total_leads=10,
            status=JobStatus.PENDING,
        )
        assert job.status == JobStatus.PENDING
        assert job.processed == 0
        assert job.succeeded == 0
        assert job.failed == 0
        assert job.results == []

    def test_job_progress_calculation(self):
        """Progress should be processed / total_leads as percentage."""
        job = BatchJob(
            id="test-456",
            total_leads=100,
            status=JobStatus.PROCESSING,
            processed=25,
        )
        assert job.progress == 25.0

    def test_job_progress_zero_leads(self):
        """Progress should be 0 when total_leads is 0."""
        job = BatchJob(id="test-789", total_leads=0, status=JobStatus.PENDING)
        assert job.progress == 0.0

    def test_job_progress_complete(self):
        """Progress should be 100 when all leads processed."""
        job = BatchJob(
            id="test-done",
            total_leads=50,
            status=JobStatus.COMPLETED,
            processed=50,
            succeeded=48,
            failed=2,
        )
        assert job.progress == 100.0


# ---------------------------------------------------------------------------
# Batch Processing Tests
# ---------------------------------------------------------------------------

class TestBatchProcessing:
    """Tests for the run_batch coroutine."""

    @pytest.mark.asyncio
    @patch("src.batch.qualify_lead")
    async def test_run_batch_processes_all_leads(self, mock_qualify, mock_qualification_result):
        """Should process every lead in the batch."""
        mock_qualify.return_value = mock_qualification_result
        leads = [
            {"email": f"user{i}@test.com", "company": f"Co{i}", "message": "Interested"}
            for i in range(5)
        ]

        job = BatchJob(id="batch-1", total_leads=5, status=JobStatus.PROCESSING)
        await run_batch(job, leads, concurrency=2)

        assert job.processed == 5
        assert job.succeeded == 5
        assert job.failed == 0
        assert job.status == JobStatus.COMPLETED
        assert len(job.results) == 5

    @pytest.mark.asyncio
    @patch("src.batch.qualify_lead")
    async def test_run_batch_handles_failures(self, mock_qualify, mock_qualification_result):
        """Should continue processing when individual leads fail."""
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("API timeout")
            return mock_qualification_result

        mock_qualify.side_effect = side_effect
        leads = [
            {"email": f"user{i}@test.com", "company": f"Co{i}", "message": "Hi"}
            for i in range(3)
        ]

        job = BatchJob(id="batch-2", total_leads=3, status=JobStatus.PROCESSING)
        await run_batch(job, leads, concurrency=1)

        assert job.processed == 3
        assert job.succeeded == 2
        assert job.failed == 1
        assert job.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    @patch("src.batch.qualify_lead")
    async def test_run_batch_respects_concurrency(self, mock_qualify, mock_qualification_result):
        """Should not exceed the concurrency limit."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def track_concurrency(*args, **kwargs):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1
            return mock_qualification_result

        mock_qualify.side_effect = track_concurrency
        leads = [
            {"email": f"user{i}@test.com", "company": f"Co{i}", "message": "Hi"}
            for i in range(10)
        ]

        job = BatchJob(id="batch-3", total_leads=10, status=JobStatus.PROCESSING)
        await run_batch(job, leads, concurrency=3)

        assert max_concurrent <= 3
        assert job.processed == 10


# ---------------------------------------------------------------------------
# Batch Summary Tests
# ---------------------------------------------------------------------------

class TestBatchSummary:
    """Tests for BatchSummary generation."""

    def test_summary_tier_breakdown(self):
        """Should correctly count leads by tier."""
        results = [
            {"email": "a@test.com", "score": 90, "tier": "HOT"},
            {"email": "b@test.com", "score": 85, "tier": "HOT"},
            {"email": "c@test.com", "score": 65, "tier": "WARM"},
            {"email": "d@test.com", "score": 40, "tier": "COLD"},
            {"email": "e@test.com", "score": 30, "tier": "COLD"},
            {"email": "f@test.com", "score": 20, "tier": "COLD"},
        ]
        summary = BatchSummary.from_results(results)
        assert summary.total == 6
        assert summary.hot == 2
        assert summary.warm == 1
        assert summary.cold == 3

    def test_summary_average_score(self):
        """Should calculate correct average score."""
        results = [
            {"email": "a@test.com", "score": 80, "tier": "HOT"},
            {"email": "b@test.com", "score": 60, "tier": "WARM"},
            {"email": "c@test.com", "score": 40, "tier": "COLD"},
        ]
        summary = BatchSummary.from_results(results)
        assert summary.average_score == 60.0

    def test_summary_empty_results(self):
        """Should handle empty results gracefully."""
        summary = BatchSummary.from_results([])
        assert summary.total == 0
        assert summary.average_score == 0.0


# ---------------------------------------------------------------------------
# API Endpoint Tests
# ---------------------------------------------------------------------------

class TestBatchEndpoints:
    """Integration tests for batch processing API endpoints."""

    @patch("src.batch.qualify_lead")
    def test_upload_csv_endpoint(self, mock_qualify, client, sample_csv_content, mock_qualification_result):
        """POST /api/batch/upload should accept CSV and return job ID."""
        mock_qualify.return_value = mock_qualification_result
        files = {"file": ("leads.csv", sample_csv_content, "text/csv")}
        response = client.post("/api/batch/upload", files=files)

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["total_leads"] == 3
        assert data["status"] == "processing"

    def test_upload_invalid_file_type(self, client):
        """Should reject non-CSV files."""
        files = {"file": ("data.json", '{"key": "value"}', "application/json")}
        response = client.post("/api/batch/upload", files=files)
        assert response.status_code == 400

    def test_upload_missing_columns(self, client, invalid_csv_missing_columns):
        """Should return 422 when required columns are missing."""
        files = {"file": ("bad.csv", invalid_csv_missing_columns, "text/csv")}
        response = client.post("/api/batch/upload", files=files)
        assert response.status_code == 422

    def test_get_job_status(self, client):
        """GET /api/batch/jobs/{id} should return job status."""
        # First create a job
        from src.batch import _jobs
        job = BatchJob(
            id="status-test",
            total_leads=10,
            status=JobStatus.COMPLETED,
            processed=10,
            succeeded=9,
            failed=1,
        )
        _jobs["status-test"] = job

        response = client.get("/api/batch/jobs/status-test")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["processed"] == 10
        assert data["progress"] == 100.0

    def test_get_nonexistent_job(self, client):
        """Should return 404 for unknown job ID."""
        response = client.get("/api/batch/jobs/does-not-exist")
        assert response.status_code == 404

    def test_get_job_results_filtered(self, client):
        """GET /api/batch/jobs/{id}/results?tier=HOT should filter by tier."""
        from src.batch import _jobs
        job = BatchJob(
            id="filter-test",
            total_leads=3,
            status=JobStatus.COMPLETED,
            processed=3,
            succeeded=3,
            results=[
                {"email": "a@t.com", "score": 90, "tier": "HOT"},
                {"email": "b@t.com", "score": 60, "tier": "WARM"},
                {"email": "c@t.com", "score": 30, "tier": "COLD"},
            ],
        )
        _jobs["filter-test"] = job

        response = client.get("/api/batch/jobs/filter-test/results?tier=HOT")
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["tier"] == "HOT"

    def test_export_csv(self, client):
        """GET /api/batch/jobs/{id}/export should return CSV download."""
        from src.batch import _jobs
        job = BatchJob(
            id="export-test",
            total_leads=2,
            status=JobStatus.COMPLETED,
            processed=2,
            succeeded=2,
            results=[
                {"email": "a@t.com", "score": 90, "tier": "HOT", "reasoning": "Great fit"},
                {"email": "b@t.com", "score": 40, "tier": "COLD", "reasoning": "Poor fit"},
            ],
        )
        _jobs["export-test"] = job

        response = client.get("/api/batch/jobs/export-test/export")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers.get("content-disposition", "")

        # Parse the CSV response
        reader = csv.DictReader(io.StringIO(response.text))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["email"] == "a@t.com"
        assert rows[0]["tier"] == "HOT"

    def test_cancel_running_job(self, client):
        """DELETE /api/batch/jobs/{id} should cancel a running job."""
        from src.batch import _jobs
        job = BatchJob(
            id="cancel-test",
            total_leads=100,
            status=JobStatus.PROCESSING,
            processed=10,
        )
        _jobs["cancel-test"] = job

        response = client.delete("/api/batch/jobs/cancel-test")
        assert response.status_code == 200
        assert _jobs["cancel-test"].status == JobStatus.CANCELLED
