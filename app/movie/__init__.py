"""Movie services with a lightweight package import for the search subprocess."""
from .cms_parser import first_play_url, parse_cms_response, split_play_url

__all__ = ["MovieController", "first_play_url", "parse_cms_response", "split_play_url"]


def __getattr__(name):
    if name == "MovieController":
        from .movie_controller import MovieController
        return MovieController
    raise AttributeError(name)
