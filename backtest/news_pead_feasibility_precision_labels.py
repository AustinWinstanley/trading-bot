# Hand-checked ground truth for the 120-headline sample drawn from
# is_earnings_result_headline()'s positive-labeled, single-symbol articles
# in state/news/alpaca_news_2026-01-01_2026-07-31.parquet
# (random_state=20260812). 0-indexed positions (matching the .reset_index()
# order the sample was drawn in) of headlines judged FALSE POSITIVES —
# i.e. the regex labeled them "earnings result released" but they are
# actually forward-looking preview/forecast articles, valuation-ratio
# commentary, or other non-result content. Everything not listed here was
# judged a true positive (a genuine earnings-result-adjacent event: an
# actual reported figure, a guidance update issued alongside/about an
# earnings release, or commentary published in clear reaction to an
# already-released result).
FALSE_POSITIVE_INDICES = {
    10,  # "A Glimpse of FormFactor's Earnings Potential" -- forward preview template
    13,  # "Earnings Outlook For Cullen/Frost Bankers" -- forward "Outlook"
    16,  # "Insights into Banco Santander Chile Q4 Earnings" -- preview template
    18,  # "Insights into Monolithic Power Systems Q4 Earnings" -- preview template
    25,  # "Analysts Say Microsoft Stock Is Deeply Undervalued Ahead of Q4 Earnings" -- "Ahead of"
    28,  # "Dan Ives Calls Jensen Huang ... Ahead Of NVDA Earnings" -- "Ahead Of"
    30,  # "Price Over Earnings Overview: Netflix" -- P/E ratio commentary, not a report
    31,  # "Vishay Intertechnology: Q4 Earnings Insights" -- preview template
    44,  # "Earnings Outlook For Lee Enterprises" -- forward "Outlook"
    46,  # "Upstart Stock Is Trending Ahead Of Q4 Earnings" -- "Ahead Of"
    48,  # "Price Over Earnings Overview: Wendy's" -- P/E ratio commentary
    57,  # "Exploring Centene's Earnings Expectations" -- forward "Expectations"
    58,  # "Earnings Outlook For Helen Of Troy" -- forward "Outlook"
    61,  # "Earnings Outlook For Delek US Hldgs" -- forward "Outlook"
    65,  # "A Look Ahead: Federated Hermes's Earnings Forecast" -- forward preview template
    68,  # "A Peek at Marriott Intl's Future Earnings" -- forward preview template
    80,  # "Examining the Future: Pennant Park Investment's Earnings Outlook" -- forward
    82,  # "John B Sanfilippo & Son's Earnings Outlook" -- forward "Outlook"
    83,  # "A Look Into Trane Technologies Inc's Price Over Earnings" -- P/E ratio commentary
    84,  # "Insights into Group 1 Automotive Q4 Earnings" -- preview template
    92,  # "An Overview of National Vision Holdings's Earnings" -- forward preview template
    104,  # "A Glimpse of Viavi Solutions's Earnings Potential" -- forward preview template
}
