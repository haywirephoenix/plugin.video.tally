"""Genre categories for the Genres menu: the same list skin.ultra shows on its search page.

Each entry is (label, params for action=genre). Genre names are TMDb's English names; movie and TV
names differ, so a category lists both ("Action" for movies, "Action & Adventure" for shows).
"""

CATEGORIES = [
    ("Action & Adventure", {"genres": ["Action", "Adventure", "Action & Adventure"]}),
    ("Anime", {"genres": ["Animation"], "language": "ja"}),
    ("Children & Family", {"genres": ["Family", "Kids"]}),
    ("Comedies", {"genres": ["Comedy"]}),
    ("Documentaries", {"genres": ["Documentary"]}),
    ("Dramas", {"genres": ["Drama"]}),
    ("Horror", {"genres": ["Horror"]}),
    ("International", {"genres": ["Drama"], "language": "ko"}),
    ("Music & Musicals", {"genres": ["Music"]}),
    ("Reality TV", {"genres": ["Reality"]}),
    ("Sci-Fi & Fantasy", {"genres": ["Science Fiction", "Fantasy", "Sci-Fi & Fantasy"]}),
    ("Sports", {"keywords": "6075"}),
    ("Thrillers", {"genres": ["Thriller", "Mystery"]}),
]


def genre_params(params):
    """A category's params as action=genre URL params (genre names joined by " / ")."""
    out = dict(params)
    if out.get("genres"):
        out["genres"] = " / ".join(out["genres"])
    return out
