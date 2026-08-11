"""Seed the 15 target companies and the people worth tracking at each.

Idempotent: re-running adds anything missing and leaves existing rows alone.

    python3 seed.py            # companies + people only
    python3 seed.py --discover # also run ATS / blog / changelog discovery
"""

import asyncio
import sys

import database as db

# (name, website, people, optional_fetchers)
# people: (name, role, track_arxiv)
SEED = [
    ("Cursor", "https://cursor.com", [
        ("Michael Truell", "Cofounder and CEO", False),
        ("Sualeh Asif", "Cofounder and CPO", False),
        ("Aman Sanger", "Cofounder", False),
        ("Arvid Lunnemark", "Cofounder", False),
    ], ["reddit"]),

    ("Thinking Machines Lab", "https://thinkingmachines.ai", [
        ("Mira Murati", "Cofounder and CEO", False),
        ("John Schulman", "Cofounder", True),
        ("Barret Zoph", "Cofounder and CTO", True),
    ], ["arxiv"]),

    ("Applied Intuition", "https://www.appliedintuition.com", [
        ("Qasar Younis", "Cofounder and CEO", False),
        ("Peter Ludwig", "Cofounder and CTO", False),
    ], []),

    ("Modal", "https://modal.com", [
        ("Erik Bernhardsson", "Founder and CEO", False),
    ], []),

    ("Decagon", "https://decagon.ai", [
        ("Jesse Zhang", "Cofounder and CEO", False),
        ("Ashwin Sreenivas", "Cofounder and CTO", False),
    ], []),

    ("Cohere", "https://cohere.com", [
        ("Aidan Gomez", "Cofounder and CEO", True),
    ], ["arxiv"]),

    ("Glean", "https://www.glean.com", [
        ("Arvind Jain", "Cofounder and CEO", False),
    ], []),

    ("LangChain", "https://www.langchain.com", [
        ("Harrison Chase", "Cofounder and CEO", False),
    ], []),

    ("Fireworks AI", "https://fireworks.ai", [
        ("Lin Qiao", "Cofounder and CEO", False),
    ], []),

    ("Cognition", "https://cognition.ai", [
        ("Scott Wu", "Cofounder and CEO", False),
        ("Walden Yan", "Cofounder", False),
    ], ["reddit"]),

    ("Ramp", "https://ramp.com", [
        ("Eric Glyman", "Cofounder and CEO", False),
        ("Karim Atiyeh", "Cofounder and CTO", False),
    ], []),

    ("Sierra", "https://sierra.ai", [
        ("Bret Taylor", "Cofounder and CEO", False),
        ("Clay Bavor", "Cofounder", False),
    ], []),

    ("ElevenLabs", "https://elevenlabs.io", [
        ("Mati Staniszewski", "Cofounder and CEO", False),
        ("Piotr Dabkowski", "Cofounder and CTO", False),
    ], ["appstore"]),

    ("Together AI", "https://www.together.ai", [
        ("Vipul Ved Prakash", "Cofounder and CEO", False),
        ("Tri Dao", "Chief Scientist", True),
    ], ["arxiv"]),

    ("Mercor", "https://mercor.com", [
        ("Brendan Foody", "Cofounder and CEO", False),
        ("Adarsh Hiremath", "Cofounder and CTO", False),
        ("Surya Midha", "Cofounder and COO", False),
    ], []),
]


def seed_companies() -> dict:
    """Insert any missing companies and people. Returns a summary."""
    db.init_db()
    db._migrate_db()

    added_companies, added_people = [], []

    for name, website, people, optional in SEED:
        existing = db.get_company_by_name(name)
        if existing:
            company_id = existing["id"]
        else:
            company_id = db.create_company(
                name=name, website=website, enabled_optional_fetchers=optional)
            added_companies.append(name)

        have = {p["name"] for p in db.list_people(company_id)}
        for person_name, role, track_arxiv in people:
            if person_name not in have:
                db.create_person(company_id, person_name, role, track_arxiv)
                added_people.append(f"{name}: {person_name}")

    return {
        "companies_added": added_companies,
        "people_added": added_people,
        "total_companies": len(db._list_companies()),
        "total_people": len(db.list_people()),
    }


async def seed_and_discover(overwrite: bool = False) -> dict:
    import discovery

    summary = seed_companies()
    companies = db._list_companies()

    results = await asyncio.gather(
        *[discovery.run_discovery(c["id"], overwrite=overwrite) for c in companies],
        return_exceptions=True,
    )

    summary["discovery"] = {}
    for company, res in zip(companies, results):
        if isinstance(res, Exception):
            summary["discovery"][company["name"]] = {"error": str(res)}
        else:
            summary["discovery"][company["name"]] = res
    return summary


if __name__ == "__main__":
    if "--discover" in sys.argv:
        out = asyncio.run(seed_and_discover(overwrite="--overwrite" in sys.argv))
        print(f"Companies: {out['total_companies']}, people: {out['total_people']}")
        print()
        for name, res in out["discovery"].items():
            if "error" in res:
                print(f"{name:24} ERROR {res['error']}")
                continue
            ats = f"{res['ats_platform']}/{res['ats_slug']}" if res["ats_slug"] else "-"
            print(f"{name:24} ats={ats:28} blog={'y' if res['blog_rss'] else '-'} "
                  f"changelog={'y' if res['changelog'] else '-'} "
                  f"yt={'y' if res['youtube_channel'] else '-'} "
                  f"gh={'y' if res['github_org'] else '-'} "
                  f"missing={','.join(res['not_found']) or 'none'}")
    else:
        out = seed_companies()
        print(f"Added {len(out['companies_added'])} companies, "
              f"{len(out['people_added'])} people.")
        print(f"Total: {out['total_companies']} companies, {out['total_people']} people.")
