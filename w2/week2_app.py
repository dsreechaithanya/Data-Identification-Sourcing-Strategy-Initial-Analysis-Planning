"""Beauty & wellness demand planning and Week 2 report generator.

Uses only the Python standard library. Run with ``python week2_app.py`` for the
desktop app, or ``python week2_app.py --help`` for the command-line interface.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import tempfile
import tkinter as tk
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Sequence
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr


APP_TITLE = "Beauty & Wellness Demand Planner"
MAX_FORECAST_MONTHS = 24
MIN_TRAINING_MONTHS = 6

SOURCES = (
    {
        "name": "Google Trends (India)",
        "url": "https://trends.google.com/trends/explore?geo=IN&q=skincare",
        "use": "Monthly relative search interest for agreed skincare terms; possible leading indicator.",
        "access": "Public web interface; export a consistent geography, category, query, and date range.",
        "frequency": "Time window selectable; values are normalized 0-100, not search counts.",
        "quality": "Useful for interest direction; sampling and normalization limit reproducibility and comparability.",
        "fit": "Medium: demand proxy only; compare with actual monthly sales and test lags.",
    },
    {
        "name": "Open Government Data Platform India",
        "url": "https://www.data.gov.in/",
        "use": "Search for household consumption, retail, population, and relevant state/district context.",
        "access": "Public catalog; individual datasets have their own download/API terms and may require an API key.",
        "frequency": "Varies by publisher and dataset; verify each dataset's update date before use.",
        "quality": "Official provenance is a strength; category detail, missingness, and revisions vary by dataset.",
        "fit": "Low-to-medium: context and segmentation, not a reliable direct measure of skincare sell-through.",
    },
    {
        "name": "MoSPI eSankhyiki / CPI",
        "url": "https://esankhyiki.mospi.gov.in/",
        "use": "Consumer price and inflation context; inspect available item, geography, and time coverage.",
        "access": "Official statistics portal; access and download formats vary by table.",
        "frequency": "CPI is generally released monthly; confirm the selected series' actual cadence and revisions.",
        "quality": "High authority for published CPI; CPI is not skincare unit sales and may be too broadly classified.",
        "fit": "Medium as a price / macroeconomic control; low as the target-demand variable.",
    },
    {
        "name": "UN Comtrade API",
        "url": "https://comtradeapi.un.org/",
        "use": "India imports/exports for carefully checked cosmetics-related HS codes; trade context.",
        "access": "Public API with request limits; registration/key and query limits can apply.",
        "frequency": "Trade data is reported periodically; reporting lags and revisions are possible.",
        "quality": "Standardized official trade reporting; HS-code boundaries and re-exports need review.",
        "fit": "Low-to-medium: trade flows are not domestic retail demand or online sales.",
    },
    {
        "name": "World Bank Indicators API (India)",
        "url": "https://api.worldbank.org/v2/country/IND/indicator/FP.CPI.TOTL.ZG?format=json",
        "use": "Annual macroeconomic context such as inflation; use only where the indicator is relevant.",
        "access": "Public API; query and reuse terms should be checked for the selected indicator.",
        "frequency": "Usually annual for this example indicator; unsuitable for month-level prediction without care.",
        "quality": "Well-documented, authoritative indicator; release lag and coarse frequency limit forecast value.",
        "fit": "Low as a monthly predictor; useful for narrative context or yearly sensitivity checks.",
    },
    {
        "name": "Amazon Reviews 2023 (UCSD)",
        "url": "https://amazon-reviews-2023.github.io/",
        "use": "Public review text, ratings, timestamps, and product metadata for relevant beauty categories.",
        "access": "Research dataset download; large files and applicable dataset / platform terms require review.",
        "frequency": "Historical snapshot, not a continuously refreshed retail feed.",
        "quality": "Substantial historical review coverage; selection bias, category mapping, and rating bias remain.",
        "fit": "Low-to-medium: review volume or sentiment may be auxiliary signals, never substitute for sales.",
    },
    {
        "name": "Open Beauty Facts API",
        "url": "https://world.openbeautyfacts.org/data",
        "use": "Product, ingredient, brand, and barcode metadata for catalog normalization.",
        "access": "Open collaborative database/API; check current API usage and database licensing terms.",
        "frequency": "Community contributions; updates are continuous but product coverage is uneven.",
        "quality": "Useful for product attributes; user-contributed fields can be incomplete or inconsistent.",
        "fit": "Low for demand measurement; medium for enriching product-level records.",
    },
    {
        "name": "Meta Ad Library",
        "url": "https://www.facebook.com/ads/library/",
        "use": "Publicly viewable ad activity as optional campaign/creative context.",
        "access": "Public search interface; programmatic access and available fields vary by region and eligibility.",
        "frequency": "Ongoing ad records, subject to platform retention and access limits.",
        "quality": "First-party platform records, but not impressions, spend, conversion, or total market advertising.",
        "fit": "Low-to-medium contextual signal; do not treat ad counts as consumer demand.",
    },
)


@dataclass
class Analysis:
    category: str
    region: str
    horizon: int
    missing_policy: str
    input_path: str | None = None
    source_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    filtered_rows: int = 0
    series: list[tuple[str, float]] = field(default_factory=list)
    gap_months: list[str] = field(default_factory=list)
    missing_filled: int = 0
    monthly_average_search: dict[str, float] = field(default_factory=dict)
    monthly_average_promo: dict[str, float] = field(default_factory=dict)
    monthly_average_price: dict[str, float] = field(default_factory=dict)
    forecast: list[tuple[str, float]] = field(default_factory=list)
    model_scores: list[tuple[str, int, float, float | None]] = field(default_factory=list)
    selected_model: str | None = None
    warnings: list[str] = field(default_factory=list)


def parse_date(value: str) -> date:
    text = value.strip()
    if not text:
        raise ValueError("date is blank")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, fmt)
            return date(parsed.year, parsed.month, 1)
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {text!r}; use YYYY-MM-DD or YYYY-MM")


def parse_number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value.strip().replace(",", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def header_map(headers: Sequence[str]) -> dict[str, str]:
    return {header.strip().casefold(): header for header in headers if header}


def load_csv(
    file_path: str,
    category: str,
    region: str,
    horizon: int,
    missing_policy: str,
) -> Analysis:
    result = Analysis(
        category=category,
        region=region,
        horizon=horizon,
        missing_policy=missing_policy,
        input_path=file_path,
    )
    monthly_demand: dict[str, float] = defaultdict(float)
    monthly_search: dict[str, list[float]] = defaultdict(list)
    monthly_promo: dict[str, list[float]] = defaultdict(list)
    monthly_price: dict[str, list[float]] = defaultdict(list)

    try:
        stream = open(file_path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValueError(f"Could not open input CSV: {exc}") from exc

    with stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("The CSV is empty or has no header row.")
        columns = header_map(reader.fieldnames)
        date_key = columns.get("date") or columns.get("month")
        demand_key = columns.get("demand") or columns.get("sales") or columns.get("units_sold")
        if not date_key or not demand_key:
            raise ValueError(
                "CSV must contain a date (or month) column and a demand "
                "(or sales / units_sold) column."
            )
        search_key = columns.get("search_interest")
        promo_key = columns.get("promotion")
        price_key = columns.get("price")
        category_key = columns.get("category")
        region_key = columns.get("region") or columns.get("country")

        for row in reader:
            result.source_rows += 1
            if category_key and str(row.get(category_key, "")).strip().casefold() != category.casefold():
                result.filtered_rows += 1
                continue
            if region_key and str(row.get(region_key, "")).strip().casefold() != region.casefold():
                result.filtered_rows += 1
                continue
            try:
                month_date = parse_date(row.get(date_key, ""))
                demand = parse_number(row.get(demand_key))
                if demand is None or demand < 0:
                    raise ValueError("demand is missing, non-numeric, or negative")
            except (ValueError, TypeError):
                result.rejected_rows += 1
                continue

            month = f"{month_date.year:04d}-{month_date.month:02d}"
            monthly_demand[month] += demand
            result.accepted_rows += 1
            if search_key:
                value = parse_number(row.get(search_key))
                if value is not None and 0 <= value <= 100:
                    monthly_search[month].append(value)
            if promo_key:
                value = parse_number(row.get(promo_key))
                if value is not None:
                    monthly_promo[month].append(value)
                else:
                    label = str(row.get(promo_key, "")).strip().casefold()
                    if label in {"yes", "true", "y", "1"}:
                        monthly_promo[month].append(1.0)
                    elif label in {"no", "false", "n", "0"}:
                        monthly_promo[month].append(0.0)
            if price_key:
                value = parse_number(row.get(price_key))
                if value is not None and value >= 0:
                    monthly_price[month].append(value)

    if not monthly_demand:
        raise ValueError(
            "No valid observations were found. Check the date and demand columns "
            "and ensure demand is numeric and non-negative."
        )

    result.monthly_average_search = {
        month: statistics.mean(values) for month, values in monthly_search.items() if values
    }
    result.monthly_average_promo = {
        month: statistics.mean(values) for month, values in monthly_promo.items() if values
    }
    result.monthly_average_price = {
        month: statistics.mean(values) for month, values in monthly_price.items() if values
    }

    observed_months = sorted(monthly_demand)
    start = month_index(observed_months[0])
    end = month_index(observed_months[-1])
    full_months = [month_from_index(index) for index in range(start, end + 1)]
    for month in full_months:
        if month not in monthly_demand:
            result.gap_months.append(month)

    if result.gap_months and missing_policy == "error":
        result.warnings.append(
            "The series has missing months. Forecasting is withheld under the "
            "'error' policy; choose zero only when missing transaction records "
            "mean no sales, or interpolate only with a documented justification."
        )
        result.series = [
            (month, monthly_demand[month])
            for month in observed_months
        ]
    else:
        observed_values = dict(monthly_demand)
        if missing_policy == "zero":
            for month in result.gap_months:
                observed_values[month] = 0.0
                result.missing_filled += 1
        elif missing_policy == "interpolate" and result.gap_months:
            known = [(month_index(m), observed_values[m]) for m in observed_months]
            for month in result.gap_months:
                target = month_index(month)
                left = max((pair for pair in known if pair[0] < target), default=None)
                right = min((pair for pair in known if pair[0] > target), default=None)
                if left and right:
                    fraction = (target - left[0]) / (right[0] - left[0])
                    observed_values[month] = left[1] + fraction * (right[1] - left[1])
                    result.missing_filled += 1
                else:
                    result.warnings.append(f"Could not interpolate boundary gap {month}.")
        result.series = [(month, observed_values[month]) for month in full_months]

    if result.rejected_rows:
        result.warnings.append(
            f"{result.rejected_rows} input row(s) were excluded for invalid dates "
            "or missing, non-numeric, non-finite, or negative demand."
        )
    if len(result.series) < MIN_TRAINING_MONTHS:
        result.warnings.append(
            f"Only {len(result.series)} usable monthly observation(s) are available; "
            f"at least {MIN_TRAINING_MONTHS} are required before producing a forecast."
        )
    if result.series and not result.gap_months and len(result.series) < 24:
        result.warnings.append(
            "Fewer than 24 consecutive months are available, so annual seasonality "
            "cannot be robustly established."
        )
    return result


def month_index(month: str) -> int:
    year, number = (int(part) for part in month.split("-"))
    return year * 12 + number - 1


def month_from_index(index: int) -> str:
    year, zero_month = divmod(index, 12)
    return f"{year:04d}-{zero_month + 1:02d}"


def linear_prediction(values: Sequence[float], target_index: int) -> float:
    count = len(values)
    if count == 1:
        return values[0]
    x_mean = (count - 1) / 2
    y_mean = statistics.mean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(count))
    slope = (
        sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
        / denominator
        if denominator
        else 0.0
    )
    return max(0.0, y_mean + slope * (target_index - x_mean))


def forecast_value(model: str, training: Sequence[float], offset: int) -> float:
    if model == "last-value":
        return training[-1]
    if model == "linear-trend":
        return linear_prediction(training, len(training) - 1 + offset)
    if model == "seasonal-naive":
        if len(training) >= 12:
            return training[-12 + ((offset - 1) % 12)]
    raise ValueError(f"Model {model!r} is not available for this training window.")


def run_forecast(result: Analysis) -> None:
    if result.gap_months and result.missing_policy == "error":
        return
    values = [value for _, value in result.series]
    if len(values) < MIN_TRAINING_MONTHS or result.horizon < 1:
        return

    holdout = min(6, max(3, math.ceil(len(values) * 0.2)))
    first_test = len(values) - holdout
    candidates = ["last-value", "linear-trend"]
    if first_test >= 12:
        candidates.append("seasonal-naive")
    scored: list[tuple[str, int, float, float | None]] = []
    for model in candidates:
        errors: list[float] = []
        absolute_actuals = 0.0
        for index in range(first_test, len(values)):
            training = values[:index]
            if model == "seasonal-naive" and len(training) < 12:
                continue
            predicted = forecast_value(model, training, index - first_test + 1)
            errors.append(abs(values[index] - predicted))
            absolute_actuals += abs(values[index])
        if len(errors) >= 3:
            mae = statistics.mean(errors)
            wape = sum(errors) / absolute_actuals * 100 if absolute_actuals else None
            scored.append((model, len(errors), mae, wape))
    result.model_scores = scored
    if scored:
        result.selected_model = min(scored, key=lambda row: (row[2], row[1] * -1))[0]
        last_index = month_index(result.series[-1][0])
        training = values
        result.forecast = [
            (
                month_from_index(last_index + step),
                round(forecast_value(result.selected_model, training, step), 4),
            )
            for step in range(1, result.horizon + 1)
        ]
    else:
        result.warnings.append(
            "Insufficient backtest coverage to select a model reliably. "
            "No forecast is presented; collect a longer, continuous series."
        )


def pearson_correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    left_dev = [value - left_mean for value in left]
    right_dev = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_dev)
        * sum(value * value for value in right_dev)
    )
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(left_dev, right_dev)) / denominator


def seasonality_table(result: Analysis) -> list[tuple[str, float, float | None]]:
    by_month: dict[int, list[float]] = defaultdict(list)
    for month, demand in result.series:
        by_month[int(month[-2:])].append(demand)
    overall = statistics.mean(value for _, value in result.series) if result.series else 0.0
    return [
        (
            f"{month:02d}",
            statistics.mean(values),
            statistics.mean(values) / overall if overall else None,
        )
        for month, values in sorted(by_month.items())
    ]


def search_correlations(result: Analysis) -> tuple[int, float | None, float | None]:
    demand = dict(result.series)
    contemporaneous: list[tuple[float, float]] = []
    lagged: list[tuple[float, float]] = []
    for month, interest in result.monthly_average_search.items():
        if month in demand:
            contemporaneous.append((interest, demand[month]))
        next_month = month_from_index(month_index(month) + 1)
        if next_month in demand:
            lagged.append((interest, demand[next_month]))
    return (
        len(contemporaneous),
        pearson_correlation(
            [pair[0] for pair in contemporaneous], [pair[1] for pair in contemporaneous]
        ),
        pearson_correlation([pair[0] for pair in lagged], [pair[1] for pair in lagged]),
    )


def make_document_xml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/>"
        '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" '
        'w:header="540" w:footer="540" w:gutter="0"/></w:sectPr></w:body></w:document>'
    )


class DocxReport:
    """Small standard-library OOXML writer for a genuine, single-file DOCX report."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.links: list[str] = []

    def paragraph(self, text: str, style: str = "Normal") -> None:
        safe = escape(str(text))
        self.parts.append(
            f'<w:p><w:pPr><w:pStyle w:val="{escape(style)}"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{safe}</w:t></w:r></w:p>'
        )

    def heading(self, text: str, level: int = 1) -> None:
        self.paragraph(text, f"Heading{level}")

    def hyperlink(self, label: str, url: str) -> None:
        link_id = f"rId{self.links.__len__() + 1}"
        self.links.append(
            f'<Relationship Id="{link_id}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            f'Target={quoteattr(url)} TargetMode="External"/>'
        )
        self.parts.append(
            '<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr>'
            f'<w:hyperlink r:id="{link_id}"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/>'
            f'</w:rPr><w:t>{escape(label)}</w:t></w:r></w:hyperlink></w:p>'
        )

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
        all_rows = [headers, *[tuple(row) for row in rows]]
        if not all_rows:
            return
        column_count = len(headers)
        grid = "".join("<w:gridCol w:w=\"1800\"/>" for _ in range(column_count))
        xml_rows: list[str] = []
        for row_index, values in enumerate(all_rows):
            cells: list[str] = []
            padded = list(values) + [""] * max(0, column_count - len(values))
            for value in padded[:column_count]:
                text = escape(str(value)).replace("\n", " ")
                if row_index == 0:
                    shading = '<w:shd w:fill="17365D"/>'
                    color = '<w:color w:val="FFFFFF"/>'
                    bold = "<w:b/>"
                else:
                    shading = '<w:shd w:fill="F2F6FA"/>' if row_index % 2 == 0 else ""
                    color = ""
                    bold = ""
                cells.append(
                    '<w:tc><w:tcPr><w:tcW w:w="1800" w:type="dxa"/>'
                    f"{shading}</w:tcPr><w:p><w:pPr><w:spacing w:after=\"40\"/>"
                    f"</w:pPr><w:r><w:rPr>{bold}{color}<w:sz w:val=\"16\"/>"
                    f"</w:rPr><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p></w:tc>"
                )
            xml_rows.append(f"<w:tr>{''.join(cells)}</w:tr>")
        self.parts.append(
            '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
            '<w:tblW w:w="0" w:type="auto"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{''.join(xml_rows)}</w:tbl>"
        )

    def save(self, output_path: str) -> None:
        relationships = "".join(self.links)
        document_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rIdStyles" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            f"{relationships}</Relationships>"
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            "</Types>"
        )
        root_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
            'Target="docProps/app.xml"/>'
            "</Relationships>"
        )
        styles = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
            '<w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
            '<w:sz w:val="20"/></w:rPr><w:pPr><w:spacing w:after="120"/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
            '<w:basedOn w:val="Normal"/><w:qFormat/><w:rPr><w:b/>'
            '<w:color w:val="17365D"/><w:sz w:val="30"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>'
            '<w:basedOn w:val="Normal"/><w:qFormat/><w:rPr><w:b/>'
            '<w:color w:val="2F5597"/><w:sz w:val="24"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
            '<w:basedOn w:val="Normal"/><w:rPr><w:b/>'
            '<w:color w:val="17365D"/><w:sz w:val="40"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/>'
            '<w:basedOn w:val="Normal"/><w:rPr><w:color w:val="666666"/>'
            '<w:sz w:val="24"/></w:rPr></w:style>'
            '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
            '<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
            '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
            '<w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="D9E2F3"/>'
            '<w:left w:val="single" w:sz="4" w:color="D9E2F3"/>'
            '<w:bottom w:val="single" w:sz="4" w:color="D9E2F3"/>'
            '<w:right w:val="single" w:sz="4" w:color="D9E2F3"/>'
            '<w:insideH w:val="single" w:sz="4" w:color="D9E2F3"/>'
            '<w:insideV w:val="single" w:sz="4" w:color="D9E2F3"/>'
            '</w:tblBorders></w:tblPr></w:style></w:styles>'
        )
        core = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(APP_TITLE)} - Week 2 Strategy</dc:title>"
            "<dc:creator>Beauty &amp; Wellness Data Planning Application</dc:creator>"
            "</cp:coreProperties>"
        )
        app = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>Beauty &amp; Wellness Demand Planner</Application></Properties>"
        )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("[Content_Types].xml", content_types)
            package.writestr("_rels/.rels", root_rels)
            package.writestr("word/document.xml", make_document_xml("".join(self.parts)))
            package.writestr("word/_rels/document.xml.rels", document_rels)
            package.writestr("word/styles.xml", styles)
            package.writestr("docProps/core.xml", core)
            package.writestr("docProps/app.xml", app)


def fmt_number(value: float | None, places: int = 2) -> str:
    return "Not available" if value is None else f"{value:,.{places}f}"


def build_report(
    category: str,
    region: str,
    horizon: int,
    output_path: str,
    analysis: Analysis | None = None,
) -> None:
    report = DocxReport()
    report.paragraph("WEEK 2 | DATA SCIENCE PROJECT", "Subtitle")
    report.paragraph("Data Sourcing Strategy & Initial Analysis Plan", "Title")
    report.paragraph(f"Business focus: monthly {category} demand in {region}", "Subtitle")
    report.paragraph(
        "Prepared as a practical sourcing, data-quality, and exploratory-analysis plan. "
        "Public sources listed here are candidate sources; confirm current terms, "
        "availability, and exact series definitions before collection."
    )

    report.heading("1. Executive summary")
    report.paragraph(
        f"Business problem: build a defensible monthly demand baseline and short-horizon "
        f"forecast for {category} in {region}, then test whether public indicators "
        "improve that forecast beyond a simple historical-sales baseline."
    )
    report.paragraph(
        "The target must be observed sell-through (units or orders per month) from an "
        "authorized point-of-sale, commerce, or distributor extract. Public interest, "
        "reviews, advertising, prices, and macroeconomic series are supporting signals, "
        "not ground truth for product sales. The major feasibility risk is that no "
        "general public source guarantees representative, monthly India skincare sales."
    )
    if analysis:
        period = (
            f"{analysis.series[0][0]} to {analysis.series[-1][0]}"
            if analysis.series
            else "no valid analysis period"
        )
        report.table(
            ["Dataset check", "Observed result"],
            [
                ("Input", Path(analysis.input_path).name if analysis.input_path else "Not supplied"),
                ("Rows read / accepted / invalid / out of scope",
                 f"{analysis.source_rows} / {analysis.accepted_rows} / {analysis.rejected_rows} / {analysis.filtered_rows}"),
                ("Usable monthly series", f"{len(analysis.series)} months ({period})"),
                ("Missing months / filled", f"{len(analysis.gap_months)} / {analysis.missing_filled}"),
                ("Configured forecast horizon", f"{analysis.horizon} month(s)"),
                ("Backtest-selected model", analysis.selected_model or "Not selected"),
            ],
        )
        if analysis.series:
            values = [value for _, value in analysis.series]
            report.paragraph(
                f"Observed monthly demand: mean {fmt_number(statistics.mean(values))}, "
                f"median {fmt_number(statistics.median(values))}, minimum "
                f"{fmt_number(min(values))}, maximum {fmt_number(max(values))}, "
                f"population standard deviation {fmt_number(statistics.pstdev(values))}."
            )
            optional_rows = [
                (
                    "Search-interest index",
                    len(analysis.monthly_average_search),
                    statistics.mean(analysis.monthly_average_search.values())
                    if analysis.monthly_average_search else None,
                ),
                (
                    "Promotion measure (mean monthly row value)",
                    len(analysis.monthly_average_promo),
                    statistics.mean(analysis.monthly_average_promo.values())
                    if analysis.monthly_average_promo else None,
                ),
                (
                    "Price",
                    len(analysis.monthly_average_price),
                    statistics.mean(analysis.monthly_average_price.values())
                    if analysis.monthly_average_price else None,
                ),
            ]
            available = [row for row in optional_rows if row[1]]
            if available:
                report.table(
                    ["Optional variable", "Months available", "Mean"],
                    [(name, count, fmt_number(mean)) for name, count, mean in available],
                )
        for warning in analysis.warnings:
            report.paragraph(f"Data-quality note: {warning}")
    else:
        report.paragraph(
            "No transaction CSV was supplied. This report is the sourcing and analysis "
            "plan; the application will add data-quality results and forecast metrics "
            "when a valid CSV is analyzed."
        )

    report.heading("2. Business question, scope, and decision")
    report.paragraph(
        f"Research question: How accurately can monthly {category} demand in {region} "
        "be forecast from historical sales, and do search interest, price/promotion "
        "context, or other public indicators provide measurable incremental value?"
    )
    report.table(
        ["Design element", "Definition for this project"],
        [
            ("Target", "Monthly units sold or orders; choose one definition and keep it consistent."),
            ("Unit of analysis", "One category/product, geography, and calendar month."),
            ("Geography / category", f"{region} / {category}; narrow to channel or region if data allows."),
            ("Forecast horizon", f"{horizon} month(s), configurable in the application."),
            ("Primary use", "Inventory and campaign planning; forecast is decision support, not a guarantee."),
            ("Known constraint", "Public indicators do not replace representative sell-through history."),
        ],
    )

    report.heading("3. Candidate data sources and assessment")
    report.paragraph(
        "Use the first source below as the target series when access is authorized. "
        "Acquire only the fields and date range needed. Confirm license, terms, "
        "geographic/category definitions, reporting dates, update cadence, and revision "
        "policy at collection time."
    )
    report.paragraph(
        "For uploaded CSVs, filter to the selected category/geography first or include "
        "optional category and region (or country) columns that exactly match the app "
        "settings. Valid demand rows are summed within each calendar month; optional "
        "search, promotion, and price fields are averaged. Review transaction IDs for "
        "duplicates before upload because the app cannot deduplicate records."
    )
    source_rows: list[Sequence[object]] = [
        (
            "Authorized commerce / POS export (primary; organization-provided)",
            "Units or orders, transaction date, SKU/category, region/channel; price, "
            "discount, promotion, stock availability where permitted.",
            "Private owner export; weekly or daily records can be rolled up monthly.",
            "High direct relevance if definitions, returns, stock-outs, and coverage are documented.",
            "Best target source; not public. Obtain permission and document coverage.",
        ),
    ]
    source_rows.extend(
        (item["name"], item["use"], item["access"] + " " + item["frequency"],
         item["quality"], item["fit"])
        for item in SOURCES
    )
    report.table(
        ["Source", "Potential fields / purpose", "Access and frequency", "Quality / reliability", "Relevance / limitation"],
        source_rows,
    )
    report.heading("Source links", 2)
    for item in SOURCES:
        report.hyperlink(item["name"], item["url"])

    report.heading("4. Data sourcing strategy and collection runbook")
    report.paragraph(
        "Use a tiered strategy: obtain the sales target first, then add a small number "
        "of aligned explanatory sources only if they can be legally and consistently "
        "collected. Public-source pages below are discovery starting points, not "
        "confirmation that a suitable dataset or field is currently available."
    )
    report.table(
        ["Priority / source", "Collection example", "Keep / reject decision"],
        [
            (
                "1. Authorized POS, retailer, or commerce export (target)",
                "Request 24-36+ consecutive months at daily or weekly grain, by SKU, "
                "with transaction date, net units, channel/region, returns, price, "
                "promotion, and stock availability where approved.",
                "Use as the target only after the owner confirms population coverage, "
                "unit definition, returns treatment, and permission to analyze.",
            ),
            (
                "2. Google Trends (optional leading signal)",
                "For example, pre-register a small set such as 'skincare' and "
                "'moisturizer'; set geography to India and keep category, search type, "
                "time window, and retrieval date fixed. Save each export and settings.",
                "Retain as a relative index, not a search count. Do not compare index "
                "levels from separate query batches as though they share a scale.",
            ),
            (
                "3. MoSPI CPI or a named data.gov.in series (context)",
                "Select one exact table/series only after confirming its item scope, "
                "geography, units, cadence, publication date, and revision notes.",
                "Keep only if the definition and frequency plausibly match the "
                "business question; annual or broad inflation data is contextual, "
                "not a monthly skincare-sales target.",
            ),
            (
                "4. Reviews, trade, product catalog, or advertising (optional)",
                "Pilot a small sample; document category mapping, date coverage, "
                "access method, terms, and known selection bias before collection.",
                "Include only if it adds measurable value and lawful access is clear. "
                "Do not scrape restricted data or treat review/ad counts as sales.",
            ),
        ],
    )
    report.heading("4.1 Collection and reproducibility steps", 2)
    for item in [
        "1) Approve the decision and target definition: units sold (recommended) versus orders or revenue; gross versus net of returns; category/SKU mapping; India/channel scope; calendar-month and time-zone rule; and forecast issue date.",
        "2) Ask the data owner for an authorized extract and a field dictionary. Request transaction/order ID for duplicate checks, product and location keys, transaction date, quantity, return quantity, net selling price, discount/promotion, and in-stock or availability status. Request a control total for reconciliation.",
        "3) Preserve the received file unchanged as a read-only raw snapshot. Record source owner, extraction timestamp, covered dates, row count, file checksum, access approval, permitted use, and any known exclusions in a source register.",
        "4) Profile date and key coverage before aggregation. Resolve duplicate IDs, returns, SKU/category mapping, cancelled orders, currency, taxes, stock-outs, and missing trading days with the owner; log each rule rather than silently changing rows.",
        "5) Download public indicators only after the target period and geography are fixed. Save exact query/series identifiers, geography, filters, request or download date, observation period, release/vintage date, units, and terms. Keep the raw response beside the register entry.",
        "6) Produce a separate cleaned monthly table from raw inputs using a versioned script. Aggregate target units by month after validation; aggregate price or promotion using a defined weighting/meaning, and keep public indicators as separate source tables until keys and timing are compatible.",
        "7) Join using explicit month, geography, category, and channel keys. Preserve missingness and publication/vintage dates; for forecasting, exclude any predictor that would not have been available at the forecast issue date.",
        "8) Store credentials outside source code, restrict access to sensitive transaction files, minimize personal data, and follow source licenses, API terms, rate limits, retention, and attribution requirements.",
    ]:
        report.paragraph(item)
    report.heading("4.2 Example data dictionary and file layout", 2)
    report.paragraph(
        "Illustrative schema only; field names must be mapped to the actual source "
        "dictionary. Keep transaction-level raw data separate from the model-ready "
        "monthly table."
    )
    report.table(
        ["Field", "Grain / example", "Rule before use"],
        [
            ("transaction_id", "One source order line; e.g. T-1042", "Required for duplicate investigation; never expose personal identifiers in the report."),
            ("transaction_date", "Source timestamp; e.g. 2025-01-14", "Parse timezone and assign month using the approved India business-calendar rule."),
            ("sku / category", "Product key and mapped category; e.g. S-01 / skincare", "Use a dated, documented mapping; keep unmapped records visible in the QA log."),
            ("region / channel", "Approved geography and sales channel", "Normalize spelling and levels; don't combine channels unless scope permits."),
            ("net_units", "Units after the agreed returns rule; e.g. 5", "Finite numeric value; negative returns handled by the approved net/gross rule."),
            ("net_price / promotion / in_stock", "Currency per unit / flag or discount / availability", "Standardize currency and definitions; retain missing flags and avoid invented values."),
            ("month / source_id / release_date", "Monthly key and lineage metadata", "Retain source and publication timing through aggregation and joins."),
        ],
    )
    report.paragraph(
        "Aggregation example (illustrative, not observed data): two validated "
        "January transaction lines for the same scope with 5 and 3 net units become "
        "8 monthly units. A January Trends value remains a separate monthly signal; "
        "it is not added to units. Keep the raw rows so the monthly total can be "
        "traced and reconciled."
    )
    report.paragraph(
        "Suggested deliverable layout: raw/ (unchanged source snapshots), "
        "source_register.csv (source, owner, exact identifier/query, terms, coverage, "
        "retrieval and release dates), cleaned/monthly_demand.csv (one row per "
        "month/category/region/channel), qa_log.csv (check, affected rows/months, "
        "decision, owner, date), and report.docx. Do not put credentials in these files."
    )

    report.heading("5. Proposed hypotheses and tests")
    report.paragraph(
        "The Week 1 brief names example problem types but does not provide prewritten "
        "hypotheses. The following are therefore proposed, testable hypotheses for the "
        "selected demand-forecasting problem; replace them with the exact Week 1 wording "
        "if it differs."
    )
    report.table(
        ["Hypothesis", "Variables and method", "Evidence and caveat"],
        [
            (
                "H1: Google search interest is positively associated with monthly skincare sales, potentially with a one-month lead.",
                "Monthly actual units and consistently collected Trends index; scatter plot, Pearson and Spearman correlation, then compare lag 0 and lag 1. Test on an aligned time window.",
                "Report sample size and confidence intervals; association is not causation and Trends is normalized.",
            ),
            (
                "H2: Demand varies by calendar month after accounting for trend.",
                "Month-of-year, demand, year; seasonal plot and seasonal index. With multiple years, use a time-aware regression or Kruskal-Wallis/ANOVA as an exploratory check.",
                "Require at least two full annual cycles (preferably more); control trend and changing distribution.",
            ),
            (
                "H3: Promotion periods have different average demand from non-promotion periods.",
                "Promotion flag/discount, units, in-stock status; compare groups and use Welch's t-test or a suitable count/time-series regression.",
                "Promotions are not randomized; price, stock-outs, channel mix, and seasonality confound the comparison.",
            ),
            (
                "H4: A model using only history can be beaten by a model adding public signals.",
                "Expanding-window or rolling-origin backtesting; compare MAE and WAPE against last-value and seasonal-naive baselines.",
                "Use only signals published by the forecast date; retain a final untouched time-based test period.",
            ),
        ],
    )
    if analysis and analysis.monthly_average_search:
        pair_count, same_month, one_month_lead = search_correlations(analysis)
        report.paragraph(
            f"Optional signal check on uploaded data: {pair_count} aligned month(s); "
            f"Pearson correlation (search interest vs same-month demand) = "
            f"{fmt_number(same_month, 3)}; search interest vs following-month demand = "
            f"{fmt_number(one_month_lead, 3)}. This is exploratory, not a causal or "
            "statistical-significance claim."
        )

    report.heading("6. Initial exploratory analysis plan")
    report.paragraph(
        "Run the checks in order and save each output before modelling. The objective "
        "is to understand coverage and measurement first, describe the observed demand "
        "second, and only then test whether signals or forecasts are useful."
    )
    report.table(
        ["Stage", "Concrete analysis", "Output and interpretation"],
        [
            (
                "1. Shape and coverage",
                "Count rows and distinct IDs; report min/max date, months expected vs present, "
                "category/region/channel levels, daily coverage, and source release dates.",
                "Source inventory and month-by-month coverage calendar. Investigate gaps "
                "before deciding whether an absent month means zero or missing data.",
            ),
            (
                "2. Target distribution",
                "For units by month calculate n, sum, mean, median, standard deviation, "
                "IQR, min/max, and selected percentiles (25th/75th/95th). State whether "
                "statistics describe monthly totals or transaction averages.",
                "Summary table plus histogram/box plot. Example: a large mean-median gap "
                "suggests skew or spikes; check events and data errors before transforming.",
            ),
            (
                "3. Trend and seasonality",
                "Plot monthly totals and a 3-month rolling mean; compute year-over-year "
                "change and month-of-year averages/indices. Compare same months across "
                "years; require at least two complete annual cycles for an initial "
                "seasonality claim.",
                "Annotated time plot and seasonal profile. Mark promotions, stock-outs, "
                "assortment changes, and incomplete edge months; do not infer seasonality "
                "from one year.",
            ),
            (
                "4. Segment and explain variation",
                "Repeat summaries by channel, region, and product only when definitions "
                "are stable. Compare promotion vs non-promotion and price bands; report "
                "group counts and in-stock coverage.",
                "Small-multiple trend/box plots and a segment table. Label small samples; "
                "observational promotion differences are not causal effects.",
            ),
            (
                "5. Candidate signals",
                "Align Trends and other predictors on month and geography; report paired "
                "sample count; compare same-month and 1-month lead Pearson and Spearman "
                "correlation; inspect scatter/time plots and release timing.",
                "Correlation/lag table and plots. For example, a positive lag-1 association "
                "is a hypothesis for backtesting, not proof that searches cause sales.",
            ),
            (
                "6. Baseline and forecast validation",
                "Compare last-value and seasonal-naive forecasts using chronological "
                "rolling-origin folds; report MAE = mean absolute error and WAPE = "
                "100 x sum(abs(error))/sum(abs(actual)). Keep the latest period untouched "
                "for final evaluation.",
                "Actual-vs-predicted plot, error table by horizon, and residual time plot. "
                "If actual totals sum to zero, report WAPE as unavailable; never random-split "
                "time series.",
            ),
            (
                "7. Decision and limitations",
                "Compare public-signal model errors with the sales-only baseline on the "
                "same folds; document data exclusions, unavailable features, and forecast "
                "issue date.",
                "One-page decision note: retain a signal only if it improves out-of-time "
                "error consistently and is available when decisions are made.",
            ),
        ],
    )
    report.paragraph(
        "Illustrative calculation: if actual monthly units are [100, 120] and forecasts "
        "are [90, 130], absolute errors are [10, 10], so MAE = 10 units and WAPE = "
        "100 x 20/220 = 9.1%. This is a worked example only, not a result from the "
        "internship dataset."
    )
    report.paragraph(
        "The application currently computes row acceptance/filter counts, invalid "
        "demand/date counts, missing-month checks, basic monthly summaries, a seasonal "
        "profile, optional search correlations, and simple holdout baseline metrics. "
        "It does not yet perform transaction-ID deduplication, render plots, validate "
        "source licenses, or run formal significance tests; perform and document those "
        "checks in the approved analysis environment."
    )
    if analysis and analysis.series:
        report.heading("Uploaded dataset: exploratory results", 2)
        report.table(
            ["Statistic", "Value"],
            [
                ("Usable months", len(analysis.series)),
                ("Mean monthly demand", fmt_number(statistics.mean(v for _, v in analysis.series))),
                ("Median monthly demand", fmt_number(statistics.median(v for _, v in analysis.series))),
                ("Standard deviation (population)", fmt_number(statistics.pstdev(v for _, v in analysis.series))),
                ("Minimum / maximum", f"{fmt_number(min(v for _, v in analysis.series))} / {fmt_number(max(v for _, v in analysis.series))}"),
                ("Missing month policy", analysis.missing_policy),
            ],
        )
        report.heading("Month-of-year summary (text-bar visualization)", 2)
        season_rows = seasonality_table(analysis)
        maximum = max((avg for _, avg, _ in season_rows), default=0.0)
        report.table(
            ["Month", "Average demand", "Index vs overall average", "Relative bar"],
            [
                (
                    month,
                    fmt_number(average),
                    fmt_number(index, 3) if index is not None else "N/A",
                    "#" * min(30, round(30 * average / maximum)) if maximum else "",
                )
                for month, average, index in season_rows
            ],
        )
        if analysis.forecast:
            report.heading("Baseline forecast and time-based validation", 2)
            report.paragraph(
                f"Selected by lowest holdout MAE among eligible simple candidates: "
                f"{analysis.selected_model}. These are baseline forecasts without "
                "prediction intervals; do not use as guaranteed sales or inventory quantities."
            )
            report.table(
                ["Backtest model", "Months tested", "MAE", "WAPE (%)"],
                [
                    (name, count, fmt_number(mae), fmt_number(wape))
                    for name, count, mae, wape in analysis.model_scores
                ],
            )
            report.table(
                ["Forecast month", "Predicted demand"],
                [(month, fmt_number(value)) for month, value in analysis.forecast],
            )

    report.heading("7. Data quality assessment and preprocessing plan")
    report.paragraph(
        "Profile raw data before filtering or aggregation, then rerun checks on the "
        "cleaned monthly table. The thresholds below are proposed starting gates, not "
        "universal standards: agree them with the data owner and assignment supervisor "
        "before evaluating the actual dataset. Never turn a failed check into an "
        "unrecorded deletion or imputation."
    )
    report.table(
        ["Check", "Example test / initial gate", "Action and evidence to record"],
        [
            (
                "Completeness and period coverage",
                "Compare observed months with the approved expected calendar and source "
                "coverage. Investigate every missing month; target periods needed for a "
                "forecast should be complete or explicitly excluded.",
                "Classify each gap as no activity, feed failure, closed channel, or unknown. "
                "Zero-fill only with source-owner confirmation; retain a gap and decision log.",
            ),
            (
                "Required values and parseability",
                "Date, product/category, scope keys, and target units must parse for every "
                "included row. Proposed gate: 100% valid among rows retained for the target.",
                "Quarantine failures, count and sample them, correct from source when possible, "
                "otherwise exclude with a reason and quantify the impact on monthly totals.",
            ),
            (
                "Uniqueness / duplicates",
                "Check transaction-line ID uniqueness; proposed target is zero unexplained "
                "duplicate IDs. Also inspect exact duplicate rows and repeated ID/date/SKU keys.",
                "Confirm whether duplicates are legitimate split lines or resend errors with "
                "the owner. The current app cannot deduplicate without an ID and sums rows.",
            ),
            (
                "Validity and ranges",
                "Demand must be finite; negative quantities require a documented returns rule. "
                "Check price >= 0, promotion values in the agreed set, and Trends index in 0-100.",
                "Do not silently clamp values. Correct or quarantine invalid entries; separate "
                "returns if source semantics require gross sales plus returns.",
            ),
            (
                "Category, region, and units",
                "Measure unmapped SKU/location rows and reconcile currency, unit of measure, "
                "tax, channel, month boundary, and time zone across sources.",
                "Use controlled mapping tables and version them. Proposed target: no unmapped "
                "in-scope target rows; show excluded/out-of-scope counts separately.",
            ),
            (
                "Reconciliation and representativeness",
                "Reconcile cleaned sums to owner-provided control totals by month/channel; "
                "compare store, channel, and SKU coverage with the intended population.",
                "Agree tolerance with source owner before use; record differences, exclusions, "
                "known coverage changes, and whether the data supports the decision population.",
            ),
            (
                "Outliers and structural breaks",
                "Flag unusual monthly changes (for investigation, e.g. robust IQR/MAD flags) "
                "and dates of assortment, pricing, channel, or policy changes.",
                "Verify against source/events; retain genuine spikes. Do not delete outliers "
                "automatically or calculate thresholds on future test data.",
            ),
            (
                "Stock-outs, returns, and censoring",
                "Review in-stock days, unavailable SKUs, cancellations, and returns by month.",
                "Sales during stock-outs can understate demand. Flag or exclude affected "
                "periods from specific analyses only with a documented rationale.",
            ),
            (
                "Source comparability and time leakage",
                "Check compatible category/geography/frequency; store observation month and "
                "publication/retrieval vintage for every external predictor.",
                "Trends/reviews/ads are proxies; CPI/trade may be coarse or revised. Use only "
                "information released by the forecast issue date in backtests.",
            ),
            (
                "Privacy, terms, and lineage",
                "Confirm authorization, license/API terms, retention, minimization, and access "
                "controls; each cleaned field must point to its source and transformation.",
                "Restrict raw files and credentials, preserve immutable snapshots, and record "
                "owner, download date, file version/checksum, transformation, and QA decision.",
            ),
        ],
    )
    report.heading("7.1 Example QA log entry", 2)
    report.table(
        ["Check", "Illustrative finding", "Resolution / sign-off"],
        [
            (
                "Missing month",
                "No rows for 2025-02 between observed January and March sales.",
                "Do not assume zero. Ask whether the feed was interrupted or sales were zero; "
                "record owner response. If unresolved, mark the series incomplete and withhold "
                "forecasting under the default error policy.",
            ),
            (
                "Duplicate order line",
                "Two raw rows share transaction-line ID T-1042.",
                "Compare payload and source export; deduplicate only if owner confirms resend. "
                "Log before/after row and unit totals plus approver.",
            ),
            (
                "Unmapped product",
                "SKU S-99 has no approved category mapping.",
                "Hold out of category totals until mapped by the data owner; report its row "
                "count and units as excluded, not as zero demand.",
            ),
        ],
    )
    if analysis:
        report.paragraph(
            f"Input-specific check: {analysis.source_rows} row(s) read, "
            f"{analysis.accepted_rows} accepted, {analysis.rejected_rows} invalid, "
            f"{analysis.filtered_rows} outside selected category/geography; "
            f"{len(analysis.gap_months)} missing month(s) detected; "
            f"{analysis.missing_filled} filled under the selected policy."
        )
        if analysis.gap_months:
            report.paragraph(
                "Missing month(s): " + ", ".join(analysis.gap_months[:36])
                + (" ..." if len(analysis.gap_months) > 36 else "")
            )

    report.heading("8. Milestones and reproducibility")
    report.table(
        ["Work package", "Planned effort", "Evidence / deliverable"],
        [
            ("Scope, target, and data dictionary", "3-4 hours", "Approved target definition, geography, monthly grain, variables and hypotheses."),
            ("Source research, access and licensing checks", "6-7 hours", "Source register, exact series/API identifiers, terms, access notes and feasibility."),
            ("Primary data request and collection design", "5 hours", "Authorized extract specification, coverage and data lineage."),
            ("Cleaning, integration and quality audit", "5 hours", "Reproducible monthly data, missingness/exclusion log, quality summary."),
            ("EDA and hypothesis test design", "5-6 hours", "Summary tables, time plots, hypothesis-to-variable/method mapping."),
            ("Baseline forecasting and review", "4-5 hours", "Time-based backtest against naive baselines and documented limitations."),
            ("Report, reproducibility and stakeholder review", "2-3 hours", "Final report, source links, assumptions, and next-step decision."),
            ("Total", "30-35 hours", "Effort range aligns with the assignment; adjust within the range for data-access complexity."),
        ],
    )
    report.heading("9. Limitations and next steps")
    report.paragraph(
        "This report is a plan, not evidence that the candidate sources contain a "
        "complete public India skincare sales series. The application does not call "
        "external APIs, scrape platforms, or independently validate source availability. "
        "Public-source freshness, dataset terms, and exact categories must be checked "
        "before submission or collection. The optional CSV analysis demonstrates "
        "exploratory calculations and baseline forecasting; it is not a production "
        "inventory system or a causal model."
    )
    report.paragraph(
        "Next: confirm the exact Week 1 hypothesis wording; secure an authorized "
        "monthly sell-through history; record data definitions and source dates; "
        "collect aligned public indicators; audit coverage and missingness; then "
        "compare public-signal models with a time-based baseline."
    )
    report.save(output_path)


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="beauty_demand_test_") as temp_dir:
        csv_path = Path(temp_dir) / "sample.csv"
        output_path = Path(temp_dir) / "sample.docx"
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["date", "demand", "search_interest", "promotion", "price", "category", "region"]
            )
            for index in range(36):
                month = month_from_index(month_index("2022-01") + index)
                year, number = month.split("-")
                demand = 100 + index * 2 + (12 if int(number) in (10, 11, 12) else 0)
                writer.writerow(
                    [f"{year}-{number}-15", demand, 20 + index, index % 2, 499, "skincare", "India"]
                )
            writer.writerow(["2022-01-22", 5, 30, 0, 499, "skincare", "India"])
            writer.writerow(["2022-01-22", 900, 30, 0, 499, "haircare", "India"])
            writer.writerow(["not-a-date", 10, 20, 0, 499, "skincare", "India"])
            writer.writerow(["2025-02-01", -2, 20, 0, 499, "skincare", "India"])
        data = load_csv(str(csv_path), "skincare", "India", 3, "error")
        run_forecast(data)
        if data.accepted_rows != 37 or data.rejected_rows != 2 or data.filtered_rows != 1:
            raise AssertionError("CSV row validation did not produce expected counts.")
        if len(data.series) != 36 or data.gap_months:
            raise AssertionError("Monthly aggregation produced an unexpected series.")
        if data.series[0][1] != 105:
            raise AssertionError("Monthly demand rows were not summed correctly.")
        if len(data.forecast) != 3 or not data.model_scores:
            raise AssertionError("Forecast/backtest did not produce expected results.")
        gap_path = Path(temp_dir) / "gap.csv"
        gap_path.write_text(
            "date,demand\n2023-01,10\n2023-03,30\n2023-04,40\n"
            "2023-05,50\n2023-06,60\n2023-07,70\n",
            encoding="utf-8",
        )
        gap_data = load_csv(str(gap_path), "skincare", "India", 1, "error")
        run_forecast(gap_data)
        if gap_data.forecast or gap_data.gap_months != ["2023-02"]:
            raise AssertionError("Missing-month error policy did not withhold the forecast.")
        zero_data = load_csv(str(gap_path), "skincare", "India", 1, "zero")
        if zero_data.series[1] != ("2023-02", 0.0):
            raise AssertionError("Zero-fill policy did not fill the missing month.")
        build_report("skincare", "India", 3, str(output_path), data)
        with zipfile.ZipFile(output_path) as package:
            names = set(package.namelist())
            required = {
                "[Content_Types].xml",
                "word/document.xml",
                "word/styles.xml",
                "word/_rels/document.xml.rels",
            }
            if not required.issubset(names) or package.testzip() is not None:
                raise AssertionError("Generated DOCX package is incomplete or corrupt.")
            for name in names:
                if name.endswith(".xml") or name.endswith(".rels"):
                    ElementTree.fromstring(package.read(name))
            document = package.read("word/document.xml").decode("utf-8")
            if "Data Sourcing Strategy" not in document or "Forecast month" not in document:
                raise AssertionError("Generated report is missing expected sections.")
        print("Self-test passed: CSV validation, monthly aggregation, backtest, forecast, and DOCX package.")


class DemandPlannerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("760x470")
        self.csv_path = tk.StringVar()
        self.output_path = tk.StringVar(
            value=str(Path.cwd() / "week2_data_sourcing_strategy.docx")
        )
        self.category = tk.StringVar(value="skincare")
        self.region = tk.StringVar(value="India")
        self.horizon = tk.StringVar(value="3")
        self.missing_policy = tk.StringVar(value="error")
        self.status = tk.StringVar(
            value="Generate a sourcing plan now, or optionally add a monthly sales CSV for EDA and a baseline forecast."
        )
        self._build()

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_TITLE, font=("Segoe UI", 17, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )
        ttk.Label(
            frame,
            text="Create a Word-ready Week 2 strategy and optionally analyze your own monthly demand CSV.",
            wraplength=700,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 15))
        self._file_row(frame, 2, "Input CSV (optional)", self.csv_path, self._browse_csv)
        self._file_row(frame, 3, "Output DOCX", self.output_path, self._browse_output)
        ttk.Label(frame, text="Product / category").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.category, width=28).grid(
            row=4, column=1, sticky="w", pady=6
        )
        ttk.Label(frame, text="Country / region").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.region, width=28).grid(
            row=5, column=1, sticky="w", pady=6
        )
        ttk.Label(frame, text="Forecast horizon (months)").grid(
            row=6, column=0, sticky="w", pady=6
        )
        ttk.Spinbox(frame, from_=1, to=MAX_FORECAST_MONTHS, textvariable=self.horizon, width=8).grid(
            row=6, column=1, sticky="w", pady=6
        )
        ttk.Label(frame, text="Missing month policy").grid(
            row=7, column=0, sticky="w", pady=6
        )
        ttk.Combobox(
            frame,
            textvariable=self.missing_policy,
            values=("error", "zero", "interpolate"),
            state="readonly",
            width=16,
        ).grid(row=7, column=1, sticky="w", pady=6)
        ttk.Label(
            frame,
            text="CSV needs date (or month) and demand (or sales / units_sold) headers. "
            "Optional exact-match filters: category and region (or country). "
            "Optional measures: search_interest (0-100), promotion, price. "
            "'error' withholds forecasting when months are missing.",
            wraplength=700,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 8))
        ttk.Button(frame, text="Generate Week 2 DOCX", command=self._generate).grid(
            row=9, column=0, sticky="w", pady=8
        )
        ttk.Label(frame, textvariable=self.status, wraplength=700).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        frame.columnconfigure(1, weight=1)

    @staticmethod
    def _file_row(
        frame: ttk.Frame,
        row: int,
        label: str,
        value: tk.StringVar,
        command,
    ) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=value, width=62).grid(
            row=row, column=1, sticky="ew", pady=6
        )
        ttk.Button(frame, text="Browse...", command=command).grid(
            row=row, column=2, padx=(8, 0), pady=6
        )

    def _browse_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select monthly demand CSV",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if path:
            self.csv_path.set(path)

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Week 2 report",
            defaultextension=".docx",
            filetypes=(("Word document", "*.docx"),),
            initialfile="week2_data_sourcing_strategy.docx",
        )
        if path:
            self.output_path.set(path)

    def _generate(self) -> None:
        category = self.category.get().strip()
        region = self.region.get().strip()
        output = self.output_path.get().strip()
        try:
            horizon = int(self.horizon.get())
            if not category or not region:
                raise ValueError("Enter both a product/category and a country/region.")
            if not 1 <= horizon <= MAX_FORECAST_MONTHS:
                raise ValueError(f"Forecast horizon must be from 1 to {MAX_FORECAST_MONTHS} months.")
            if not output:
                raise ValueError("Choose an output DOCX path.")
            analysis = None
            if self.csv_path.get().strip():
                analysis = load_csv(
                    self.csv_path.get().strip(),
                    category,
                    region,
                    horizon,
                    self.missing_policy.get(),
                )
                run_forecast(analysis)
            build_report(category, region, horizon, output, analysis)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            messagebox.showerror("Could not generate report", str(exc), parent=self.root)
            self.status.set(f"Error: {exc}")
            return
        self.status.set(f"Report created: {output}")
        messagebox.showinfo("Report ready", f"Word report created successfully:\n{output}", parent=self.root)


def cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a Week 2 data sourcing strategy DOCX; optionally analyze monthly demand CSV."
    )
    parser.add_argument("--input", help="Optional CSV with date/month and demand/sales columns.")
    parser.add_argument("--output", default="week2_data_sourcing_strategy.docx", help="Output DOCX path.")
    parser.add_argument("--category", default="skincare", help="Product/category name.")
    parser.add_argument("--region", default="India", help="Country or region.")
    parser.add_argument("--horizon", type=int, default=3, help=f"Forecast months (1-{MAX_FORECAST_MONTHS}).")
    parser.add_argument(
        "--missing-policy",
        choices=("error", "zero", "interpolate"),
        default="error",
        help="How to handle absent months; default error withholds forecasting.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run a temporary-data end-to-end self-test.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = cli_parser()
    args = parser.parse_args(args_list)
    if args.self_test:
        run_self_test()
        return 0
    if not 1 <= args.horizon <= MAX_FORECAST_MONTHS:
        parser.error(f"--horizon must be from 1 to {MAX_FORECAST_MONTHS}")
    if not args.category.strip() or not args.region.strip():
        parser.error("--category and --region must not be empty")

    if not args_list:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            print(
                f"Could not start the desktop interface: {exc}\n"
                "Use command-line mode, for example:\n"
                "  python week2_app.py --output report.docx",
                file=sys.stderr,
            )
            return 2
        DemandPlannerApp(root)
        root.mainloop()
        return 0

    analysis = None
    try:
        if args.input:
            analysis = load_csv(
                args.input,
                args.category.strip(),
                args.region.strip(),
                args.horizon,
                args.missing_policy,
            )
            run_forecast(analysis)
        build_report(
            args.category.strip(),
            args.region.strip(),
            args.horizon,
            args.output,
            analysis,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Report created: {Path(args.output).resolve()}")
    if analysis:
        print(
            f"Rows accepted/invalid/out of scope: {analysis.accepted_rows}/"
            f"{analysis.rejected_rows}/{analysis.filtered_rows}; "
            f"usable months: {len(analysis.series)}; "
            f"selected model: {analysis.selected_model or 'none'}."
        )
        for warning in analysis.warnings:
            print(f"Note: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
