# Chatbot Backend Chart Payload Contract

## Purpose

This document defines what the backend should send to the chatbot frontend when an assistant response needs to render a chart.

The backend should send chart payloads as structured JSON inside Markdown fenced blocks. The frontend will detect those blocks and render the visual chart.

## Required Format

Use a fenced Markdown block with language `gpi-chart`.

````markdown
Here is the chart summary.

```gpi-chart
{
  "version": 1,
  "type": "column_chart",
  "title": "Mean liking by product",
  "x_axis": { "label": "Product" },
  "y_axis": { "label": "Mean score" },
  "metadata": {
    "source": "Charting report",
    "question_id": "QUESTION_UUID",
    "question_label": "Overall liking",
    "base_size": 128,
    "filters": [
      { "label": "Country", "value": "United States" },
      { "label": "Segment", "value": "Weekly users" }
    ],
    "products": ["Product A", "Product B"],
    "method": "Mean score comparison"
  },
  "series": [
    {
      "name": "Mean liking",
      "data": [
        { "label": "Product A", "value": 7.2 },
        { "label": "Product B", "value": 6.8 }
      ]
    }
  ]
}
```
````

The backend can include normal Markdown text before or after the chart block.

## Do Not Send

The backend must not send:

- Raw chart HTML
- JavaScript
- SVG markup
- Canvas scripts
- iframes
- secrets
- auth tokens
- hidden trace/tool output
- raw respondent PII

The frontend renders charts from JSON only.

## Common Fields

`version`
: Required. Must be `1`.

`type`
: Required. Must be one supported chart type.

`title`
: Optional chart title.

`subtitle`
: Optional chart subtitle/context.

`x_axis.label`
: Optional X axis label.

`y_axis.label`
: Optional Y axis label.

`z_axis.label`
: Required only for `pca_biplot_3d`.

`series`
: Used by most 2D charts.

`data`
: Used by simple single-series charts, heatmaps, and word clouds.

`products`
: Used by PCA charts.

`attributes`
: Used by PCA charts.

`settings.colorScheme.colors`
: Optional. Preferred way to pass the report/branding chart palette. If omitted, the frontend uses the same default chart palette used by reports.

`metadata`
: Optional chart grounding information shown with the chart so sensory scientists can verify source, sample/base, filters, products, and method.

## Chart Grounding Metadata

When sending a chart, include `metadata` whenever possible. This is displayed as a compact context strip under the chart title.

Preferred shape:

```json
{
  "metadata": {
    "source": "Charting report",
    "question_id": "QUESTION_UUID",
    "question_label": "Overall liking",
    "base_size": 128,
    "filters": [
      { "label": "Country", "value": "United States" },
      { "label": "Segment", "value": "Weekly users" }
    ],
    "products": ["Product A", "Product B"],
    "method": "PCA biplot"
  }
}
```

Supported metadata fields:

```text
source
question_id
question_label
base_size
filters
products
method
```

The frontend also accepts camelCase equivalents such as `questionId`, `questionLabel`, and `baseSize`.

Rules:

- Do not include respondent PII.
- Keep filters/product names concise.
- Send `base_size` as a number.
- Send `filters` either as an object or as `{ "label": "...", "value": "..." }` items.
- Send `products` as an array of product/sample names.

## Supported Chart Types

```text
word_cloud
bar_chart
column_chart
stacked_bar_chart
stacked_column_chart
stacked_column_bar_chart
line_chart
pie_chart
scatterplot
heatmap
spider_chart
dot_plot
histogram
test_line_chart
intensity_curve
dominance_over_time
intensity_max
intensity_time
intensity_auc
panelist_score_summary_chart
penalty_line_chart
penalty_scatterplot
pca_biplot_2d
pca_biplot_3d
```

Accepted aliases include:

```text
bar-chart
column-chart
stacked-column-bar-chart
pie-chart
line-chart
word-cloud
dot-plot
test-line-chart
difference-test-line-chart
difference_test_line_chart
intensity-curve
dominance-over-time
panelist-score-summary-chart
penalty-line-chart
penalty-scatterplot
pca-biplot
pca-biplot-3d
```

## Category Charts

Use this shape for:

```text
bar_chart
column_chart
stacked_bar_chart
stacked_column_chart
stacked_column_bar_chart
line_chart
spider_chart
histogram
intensity_max
intensity_time
intensity_auc
panelist_score_summary_chart
```

Example:

```json
{
  "version": 1,
  "type": "column_chart",
  "title": "Mean liking by product",
  "x_axis": { "label": "Product" },
  "y_axis": { "label": "Mean score" },
  "settings": {
    "colorScheme": {
      "colors": ["#4A63A8", "#2EA97D", "#E0A628", "#D95F59"]
    }
  },
  "series": [
    {
      "name": "Mean liking",
      "data": [
        { "label": "Product A", "value": 7.2 },
        { "label": "Product B", "value": 6.8 },
        { "label": "Product C", "value": 5.9 }
      ]
    }
  ]
}
```

For stacked charts, send multiple series with matching labels:

```json
{
  "version": 1,
  "type": "stacked_bar_chart",
  "title": "Purchase intent by product",
  "series": [
    {
      "name": "Would buy",
      "data": [
        { "label": "Product A", "value": 62 },
        { "label": "Product B", "value": 54 }
      ]
    },
    {
      "name": "Would not buy",
      "data": [
        { "label": "Product A", "value": 38 },
        { "label": "Product B", "value": 46 }
      ]
    }
  ]
}
```

## Line Charts

Use this shape for:

```text
line_chart
test_line_chart
intensity_curve
dominance_over_time
penalty_line_chart
```

For a difference test line chart, prefer `test_line_chart`. The frontend also accepts `difference_test_line_chart` and `difference-test-line-chart` as aliases.

Example:

```json
{
  "version": 1,
  "type": "test_line_chart",
  "title": "Difference test line chart",
  "subtitle": "Correct answers compared with the critical value",
  "x_axis": { "label": "Respondents" },
  "y_axis": { "label": "Correct answers" },
  "series": [
    {
      "name": "Correct answers",
      "data": [
        { "label": "20", "value": 11 },
        { "label": "30", "value": 17 },
        { "label": "40", "value": 22 }
      ]
    },
    {
      "name": "Critical value",
      "data": [
        { "label": "20", "value": 12 },
        { "label": "30", "value": 16 },
        { "label": "40", "value": 21 }
      ]
    }
  ]
}
```

## Pie Charts

Use `pie_chart` with one series:

```json
{
  "version": 1,
  "type": "pie_chart",
  "title": "Preferred product",
  "series": [
    {
      "name": "Preference",
      "data": [
        { "label": "Product A", "value": 45 },
        { "label": "Product B", "value": 35 },
        { "label": "Product C", "value": 20 }
      ]
    }
  ]
}
```

Example:

```json
{
  "version": 1,
  "type": "intensity_curve",
  "title": "Intensity over time",
  "x_axis": { "label": "Time" },
  "y_axis": { "label": "Intensity" },
  "series": [
    {
      "name": "Product A",
      "data": [
        { "label": "0s", "value": 0.1 },
        { "label": "10s", "value": 2.4 },
        { "label": "20s", "value": 4.1 }
      ]
    }
  ]
}
```

## Pie Chart

Use one series:

```json
{
  "version": 1,
  "type": "pie_chart",
  "title": "Preferred product",
  "series": [
    {
      "name": "Preference",
      "data": [
        { "label": "Product A", "value": 45 },
        { "label": "Product B", "value": 35 },
        { "label": "Product C", "value": 20 }
      ]
    }
  ]
}
```

## Scatterplot

Use numeric `x` and `y`.

```json
{
  "version": 1,
  "type": "scatterplot",
  "title": "Aroma vs overall liking",
  "x_axis": { "label": "Aroma" },
  "y_axis": { "label": "Overall liking" },
  "series": [
    {
      "name": "Products",
      "data": [
        { "label": "Product A", "x": 6.8, "y": 7.4 },
        { "label": "Product B", "x": 5.9, "y": 6.2 }
      ]
    }
  ]
}
```

## Penalty Scatterplot

Use numeric `x` and `y`.

```json
{
  "version": 1,
  "type": "penalty_scatterplot",
  "title": "Penalty analysis",
  "x_axis": { "label": "% respondents" },
  "y_axis": { "label": "Mean drop" },
  "series": [
    {
      "name": "Attributes",
      "data": [
        { "label": "Too sweet", "x": 32, "y": 1.4 },
        { "label": "Not creamy enough", "x": 18, "y": 0.9 }
      ]
    }
  ]
}
```

## Dot Plot

Use category labels with numeric values.

```json
{
  "version": 1,
  "type": "dot_plot",
  "title": "Mean liking dot plot",
  "x_axis": { "label": "Mean liking" },
  "series": [
    {
      "name": "Mean liking",
      "data": [
        { "label": "Product A", "value": 7.2 },
        { "label": "Product B", "value": 6.8 }
      ]
    }
  ]
}
```

## Heatmap

Use flat row/column/value data.

```json
{
  "version": 1,
  "type": "heatmap",
  "title": "Attribute correlation heatmap",
  "data": [
    { "x": "Sweetness", "y": "Sweetness", "value": 1.0 },
    { "x": "Sweetness", "y": "Aroma", "value": 0.42 },
    { "x": "Aroma", "y": "Sweetness", "value": 0.42 },
    { "x": "Aroma", "y": "Aroma", "value": 1.0 }
  ]
}
```

## Word Cloud

Use `type: "word_cloud"` inside `gpi-chart`.

```json
{
  "version": 1,
  "type": "word_cloud",
  "title": "Most common words in open answers",
  "data": [
    { "label": "fresh", "count": 42 },
    { "label": "sweet", "count": 31 },
    { "label": "natural", "count": 28 }
  ]
}
```

Grouped word clouds:

```json
{
  "version": 1,
  "type": "word_cloud",
  "title": "Open answers by product",
  "data": [
    {
      "group": "Product A",
      "values": [
        { "label": "creamy", "count": 28 },
        { "label": "smooth", "count": 22 }
      ]
    },
    {
      "group": "Product B",
      "values": [
        { "label": "fresh", "count": 34 },
        { "label": "light", "count": 19 }
      ]
    }
  ]
}
```

## PCA 2D

Use `products` and `attributes`.

```json
{
  "version": 1,
  "type": "pca_biplot_2d",
  "title": "PCA biplot",
  "x_axis": { "label": "PC1 (48%)" },
  "y_axis": { "label": "PC2 (22%)" },
  "products": [
    { "label": "Product A", "x": 1.2, "y": 0.4 },
    { "label": "Product B", "x": -0.8, "y": 0.9 }
  ],
  "attributes": [
    { "label": "Sweetness", "x": 0.7, "y": 0.2 },
    { "label": "Aroma", "x": 0.4, "y": 0.8 }
  ]
}
```

## PCA 3D

Use only when the user asks for 3D PCA or PC3 is useful. Every product and attribute point must include `x`, `y`, and `z`.

```json
{
  "version": 1,
  "type": "pca_biplot_3d",
  "title": "3D PCA map",
  "x_axis": { "label": "PC1 (48%)" },
  "y_axis": { "label": "PC2 (22%)" },
  "z_axis": { "label": "PC3 (11%)" },
  "products": [
    { "label": "Product A", "x": 1.2, "y": 0.4, "z": -0.2 },
    { "label": "Product B", "x": -0.8, "y": 0.9, "z": 0.5 }
  ],
  "attributes": [
    { "label": "Sweetness", "x": 0.7, "y": 0.2, "z": 0.1 },
    { "label": "Aroma", "x": 0.4, "y": 0.8, "z": -0.3 }
  ]
}
```

## Limits

Frontend validation currently caps chart payloads:

- Up to 8 series
- Up to 300 points per chart
- Up to 12 palette colors
- Labels are trimmed
- Unsupported chart types are not rendered

## Streaming Requirement

If the assistant response is streamed, the backend should include the complete chart fenced block in the final assistant answer. The frontend renders the chart after valid complete JSON is available.

Do not stream partial chart JSON as the final answer.
