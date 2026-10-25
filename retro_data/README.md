# Retro-signal study documents

One text file per historical document, named `TICKER__YYYY-MM-DD__slug.txt`, e.g.

    EXMP.V__2024-03-11__strategic-stake.txt

An optional first line `TITLE: ...` is used as the title. Then run:

    python -m app.cli retro --docs retro_data --out data/retro_timeline.csv

Nothing is sent to users. Add the announcement date of each deal to the CSV and count how many signals
appeared at least two weeks before it. That is the test of our main guess.
