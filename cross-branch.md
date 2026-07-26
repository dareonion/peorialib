# More toddler books — cross-branch availability

Twelve **more** best-of toddler titles (not in the earlier lists), checked across
**all six Peoria Public Library branches** on **Sunday, July 26, 2026**. Generated
from the SQLite store: `uv run report.py --matrix`.

Legend: ✓ on shelf · in-library use only ✗ out/unavailable (blank = not held there)

| Title | Call # | North | Lakeview | Main St | Lincoln | McClure | Outreach |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Blueberries for Sal — McCloskey | JP MCC |  |  |  | ✓ |  |  |
| Chugga-chugga Choo-choo — Lewis | JP LEW |  |  | · |  |  |  |
| Don't Push the Button! — Cotter | JP COT | ✓ |  |  |  |  |  |
| From Head to Toe — Carle | JP CAR | ✗ | ✓ |  | ✓ |  |  |
| I Love You to the Moon and Back — Hepworth | JP HEP | ✓ | ✓ | ✓ | ✓ |  | ✓ |
| I Stink! — McMullan | JP MCM | ✓ |  |  |  |  |  |
| Kitten's First Full Moon — Henkes | JP HEN | ✓ |  | ✓ |  | ✓ |  |
| Mouse Paint — Walsh | JP WAL |  | ✓ | ✓ |  |  |  |
| Tap the Magic Tree — Matheson | JP MAT | ✗ | ✓ | ✗ | ✓ |  |  |
| The Napping House — Wood | JP WOO |  |  | ✓* |  |  |  |
| The Pigeon Needs a Bath! — Willems | JP WIL | ✗ | ✓ | ✗ | ✓ | ✓ |  |
| The Very Quiet Cricket — Carle | JP CAR |  |  | · | ✓ |  |  |

\* *The Napping House* copy at Main St is the **audio/AV** edition (Juvenile AV Area),
not a shelf picture book.

## On the shelf at your two branches right now

**North** (4): Don't Push the Button! (`JP COT`), I Stink! (`JP MCM`), Kitten's
First Full Moon (`JP HEN`), I Love You to the Moon and Back (`JP HEP`).

**Lakeview** (5): Tap the Magic Tree (`JP MAT`), From Head to Toe (`JP CAR`), Mouse
Paint (`JP WAL`), The Pigeon Needs a Bath! (`JP WIL`), I Love You to the Moon and
Back (`JP HEP`).

*I Love You to the Moon and Back* is the standout — a board book on the shelf at
both North and Lakeview (and most other branches).

## Worth a detour to another branch

- **The Very Quiet Cricket** (Carle) and **Blueberries for Sal** (McCloskey) — on the
  shelf at **Lincoln**.
- **The Pigeon Needs a Bath!** also at **Lincoln** and **McClure** if Lakeview's is gone.

## Not easily grabbable in Peoria

- *Chugga-chugga Choo-choo* and *The Very Quiet Cricket*'s Main copy are in the
  non-circulating Children's Workroom.
- *Is Your Mama a Llama?* and *Doggies* (Boynton) are held only as **Spanish**
  editions. *The Feelings Book* (Parr), *The Way I Feel*, *Llama Llama Mad at Mama*,
  *No, David!*, *Pajama Time!*, *Ten Nine Eight*, and *Truck* (Crews) aren't in the
  Peoria collection.

---

*This scrape and all its per-branch statuses are stored in `peorialib.db` (local,
gitignored). Re-run `uv run report.py --matrix` any time to regenerate this table
from the latest data; `uv run library_lookup.py --details ...` appends new checks.*
