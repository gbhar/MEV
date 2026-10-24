"""Join features, opportunities, chosen simulation and paper-trade outcomes into one CSV for model training.
Rows without a feature vector or an unresolved paper trade are kept (outcome columns empty) so the
blocked / unresolved population is visible; filter in the notebook."""
import argparse
import csv

from sqlalchemy import select

from mev.db.models import Feature, Opportunity, PaperTrade
from mev.db.session import session

SKIP = {"id", "opportunity_id", "created_at"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dataset.csv")
    args = ap.parse_args()
    with session() as db, open(args.out, "w", newline="") as f:
        q = (select(Opportunity, Feature, PaperTrade).outerjoin(Feature, Feature.opportunity_id == Opportunity.id)
             .outerjoin(PaperTrade, PaperTrade.opportunity_id == Opportunity.id).order_by(Opportunity.id))
        w = None
        n = 0
        for opp, feat, trade in db.execute(q):
            row = {"opportunity_id": opp.id}
            for prefix, obj, cls in (("", opp, Opportunity), ("f_", feat, Feature), ("t_", trade, PaperTrade)):
                for col in cls.__table__.columns:
                    if col.name in SKIP:
                        continue
                    row[prefix + col.name] = getattr(obj, col.name) if obj is not None else ""
            if w is None:
                w = csv.DictWriter(f, fieldnames=list(row))
                w.writeheader()
            w.writerow(row)
            n += 1
    print(f"wrote {n} rows to {args.out}")


if __name__ == "__main__":
    main()
