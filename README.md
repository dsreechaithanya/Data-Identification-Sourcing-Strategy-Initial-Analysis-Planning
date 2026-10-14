Beauty & Wellness Demand Planner

A Python desktop and command-line application for planning beauty and
wellness demand analysis. It helps organize a data-sourcing strategy,
validate monthly sales data from a CSV file, explore demand patterns,
compare simple forecasting baselines, and generate a Week 2
data-sourcing and analysis-plan report in Word format.

Features

Desktop interface built with Python's tkinter.

Command-line interface for generating reports.

Reads CSV data containing monthly dates and demand/sales values.

Supports optional search-interest, promotion, price, category, and
region fields.

Checks invalid rows and missing months.

Summarizes monthly demand and provides exploratory analysis.

Compares simple forecasting approaches, including last-value,
linear-trend, and seasonal-naive baselines when sufficient data is
available.

Creates a .docx report describing data sources, collection
strategy, hypotheses, EDA plan, data-quality checks, and
limitations.

Uses Python's standard library; no third-party Python packages are
required for the application.

Requirements

Python 3.10 or later recommended.

tkinter must be available for the desktop interface. It is
included with many standard Python installations.

A CSV file is optional. The app can generate the sourcing-plan
report without one.

Project file

Save the Python source as:

week2_app.py

The uploaded Week 2 document contains the Python source code. Copy
that code into a file named week2_app.py before running the commands
below.

Run the desktop application

Open a terminal or Command Prompt in the folder containing
week2_app.py, then run:

python week2_app.py

Use the command line

Display available options:

python week2_app.py --help

Generate a report with default settings:

python week2_app.py --output week2_data_sourcing_strategy.docx

Set the product category, region, and forecast horizon:

python week2_app.py --category skincare --region India --horizon 3 --output week2_report.docx

Analyze a CSV file and generate a report:

python week2_app.py --input monthly_demand.csv --output week2_report.docx --category skincare --region India --horizon 3

Run the built-in self-test:

python week2_app.py --self-test

The exact available options and missing-month policies can be checked
with python week2_app.py --help.

CSV input format

The CSV must include a date/month column and a demand column. Accepted
column names include:

Date: date or month

Demand: demand, sales, or units_sold

Optional columns supported by the application:

search_interest --- relative search-interest index from 0 to 100

promotion --- numeric value or a yes/no-style flag

price --- non-negative price

category --- category used for filtering

region or country --- geography used for filtering

Example:

date,demand,search_interest,promotion,price,category,region
2025-01-01,120,45,0,250,skincare,India
2025-02-01,135,52,1,240,skincare,India
2025-03-01,128,49,0,250,skincare,India
2025-04-01,150,61,1,230,skincare,India
2025-05-01,142,58,0,250,skincare,India
2025-06-01,160,67,1,225,skincare,India

This is illustrative sample data only, not actual market data.
Forecasting requires at least six usable monthly observations. Longer,
continuous histories are recommended, especially when investigating
annual seasonality.

Data sources considered

The report describes possible supporting sources such as:

Google
Trends

Open Government Data Platform India

MoSPI eSankhyiki

UN Comtrade API

World Bank Indicators
API

Amazon Reviews 2023

Open Beauty Facts

Meta Ad Library

These are candidate sources, not guaranteed direct measures of monthly
product sales. Authorized point-of-sale or commerce data should be used
as the main demand target when available. Check each source's terms,
coverage, definitions, and update schedule before using it.

Important limitations

Public search interest, reviews, advertising, trade, and economic
indicators are supporting signals; they are not ground truth for
product sales.

The application does not automatically download data from the listed
external sources.

Review the source data for duplicate transactions before analysis;
the app does not deduplicate transaction IDs.

Missing months should not automatically be treated as zero sales.
Choose a missing-data policy based on the meaning of the gap and
document the decision.

Forecasts are exploratory decision-support estimates, not guaranteed
sales or inventory quantities.

Suggested repository structure

beauty-wellness-demand-planner/
├── week2_app.py
├── README.md
├── monthly_demand.csv          # optional input data
└── week2_data_sourcing_strategy.docx  # generated report

