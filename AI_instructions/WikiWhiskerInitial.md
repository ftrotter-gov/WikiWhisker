# WikiWhisker — Project Specification

## Overview

WikiWhisker is a Python-based command-line tool that uses the Wikipedia API to crawl a limited, structured subset of Wikipedia pages and extract data into a small CSV file. It is not a general-purpose web crawler; it is a narrow, purposeful data-extraction tool.

---

## Data Source

- **API**: Wikipedia's official API (not direct HTML scraping of the Wikipedia website)
- **Data structures used**: InfoBoxes and WikiData-derived fields embedded within Wikipedia pages

---

## Crawl Behavior

The crawl is **two levels deep only**:

1. **Starting page(s)**: One or more Wikipedia pages provided on the command line
2. **Secondary pages**: Pages linked from the starting page(s) that pass a link-following rule (see below)

There is **no recursive or infinite crawl**. The tool does not follow links beyond secondary pages.

---

## Configuration (Command-Line Arguments)

The tool accepts the following arguments from the command line:

| Argument | Description |
|---|---|
| Starting page(s) | One or more Wikipedia page titles or URLs to begin the crawl |
| Link-following rule | A definition of which links on the starting page should be followed to secondary pages |
| Extraction question(s) | One or more small, specific questions to answer from each secondary page |

All three of the above must be generalizable — the tool should work for different crawl targets by changing only these arguments, not the source code.

---

## Output

- Format: CSV file
- Width: 3–5 columns
- Content: A simplified taxonomy derived from answers to the extraction questions
- The specific columns are determined by the extraction questions passed as arguments

---

## What the Tool Does NOT Do

- Does not browse the web in any general-purpose sense
- Does not follow links beyond secondary pages
- Does not render JavaScript or act as a smart browser
- Does not scrape the Wikipedia website directly (uses the API only)

---

## Summary of Intended Use

The operator provides a starting Wikipedia page, a rule for which links to follow from that page, and a small question to ask of each linked page. The tool fetches those pages via the Wikipedia API, reads their InfoBox or WikiData-derived fields, answers the question for each, and writes the results to a CSV file.
