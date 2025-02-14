import html
import logging
import os

import networkx
import numpy as np
import pandas as pd
import simplejson

from hitch.helpers import get_bearing, get_db, get_dirs, haversine_np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dirs = get_dirs()

logger.info("Creating directories if they don't exist")
os.makedirs(dirs["dist"], exist_ok=True)

logger.info("Fetching points from database")
points = pd.read_sql(
    sql="select * from points where not banned order by datetime is not null desc, datetime desc",
    con=get_db(),
)

points["user_id"] = points["user_id"].astype(pd.Int64Dtype())

logger.info("Fetching duplicates from database")
duplicates = pd.read_sql("select * from duplicates where reviewed = accepted", get_db())

try:
    logger.info("Fetching users from database")
    users = pd.read_sql("select * from user", get_db())
except pd.errors.DatabaseError as err:
    logger.error("Failed to fetch users from database")
    raise Exception("Run server.py to create the user table") from err

logger.info(f"{len(points)} points currently")

# merging and transforming data
dup_rads = duplicates[["from_lon", "from_lat", "to_lon", "to_lat"]].values.T

duplicates["distance"] = haversine_np(*dup_rads)
duplicates["from"] = duplicates[["from_lat", "from_lon"]].apply(tuple, axis=1)
duplicates["to"] = duplicates[["to_lat", "to_lon"]].apply(tuple, axis=1)

duplicates = duplicates[duplicates.distance < 1.25]

dups = networkx.from_pandas_edgelist(duplicates, "from", "to")
islands = networkx.connected_components(dups)

replace_map = {}

logger.info("Processing duplicates")
for island in islands:
    parents = [node for node in island if node not in duplicates["from"].tolist()]

    if len(parents) == 1:
        for node in island:
            if node != parents[0]:
                replace_map[node] = parents[0]

logger.info(f"Currently recorded duplicate spots are represented by: ${dups}")

logger.info("Replacing duplicate points")
points[["lat", "lon"]] = points[["lat", "lon"]].apply(lambda x: replace_map.get(tuple(x), x), axis=1, raw=True)

points.loc[points.id.isin(range(1000000, 1040000)), "comment"] = (
    points.loc[points.id.isin(range(1000000, 1040000)), "comment"]
    .str.encode("cp1252", errors="ignore")
    .str.decode("utf-8", errors="ignore")
)

points["datetime"] = pd.to_datetime(points.datetime)
points["ride_datetime"] = pd.to_datetime(points.ride_datetime, errors="coerce")

rads = points[["lon", "lat", "dest_lon", "dest_lat"]].values.T

points["distance"] = haversine_np(*rads)
points["direction"] = get_bearing(*rads)

points.loc[(points.distance < 1), "dest_lat"] = None
points.loc[(points.distance < 1), "dest_lon"] = None
points.loc[(points.distance < 1), "direction"] = None
points.loc[(points.distance < 1), "distance"] = None

rounded_dir = 45 * np.round(points.direction / 45)
points["arrows"] = rounded_dir.replace(
    {
        -90: "←",
        90: "→",
        0: "↑",
        180: "↓",
        -180: "↓",
        -45: "↖",
        45: "↗",
        135: "↘",
        -135: "↙",
    }
)

rating_text = "rating: " + points.rating.astype(int).astype(str) + "/5"
destination_text = ", ride: " + np.round(points.distance).astype(str).str.replace(".0", "", regex=False) + " km " + points.arrows

points["wait_text"] = None
has_accurate_wait = ~points.wait.isnull() & ~points.datetime.isnull()
points.loc[has_accurate_wait, "wait_text"] = (
    ", wait: "
    + points.wait[has_accurate_wait].astype(int).astype(str)
    + " min"
    + (" " + points.signal[has_accurate_wait].replace({"ask": "💬", "ask-sign": "💬+🪧", "sign": "🪧", "thumb": "👍"})).fillna("")
)


def e(s):
    s2 = s.copy()
    s2.loc[~s2.isnull()] = s2.loc[~s2.isnull()].map(lambda x: html.escape(x).replace("\n", "<br>"))
    return s2


points["extra_text"] = rating_text + points.wait_text.fillna("") + destination_text.fillna("")

comment_nl = points["comment"] + "\n\n"

comment_nl.loc[(points.datetime.dt.year > 2021) & points.comment.isnull()] = ""

review_submit_datetime = points.datetime.dt.strftime(", %B %Y").fillna("")

points["username"] = pd.merge(
    left=points[["user_id"]],
    right=users[["id", "username"]],
    left_on="user_id",
    right_on="id",
    how="left",
)["username"]
points["hitchhiker"] = points["nickname"].fillna(points["username"])

points["user_link"] = ("<a href='/?user=" + e(points["hitchhiker"]) + "#filters'>" + e(points["hitchhiker"]) + "</a>").fillna(
    "Anonymous  "
    + '<i class="icon-button fa fa-hand" title="Claim this review as yours." onclick="confirmClaimReview(\'/claim-review/'
    + points["id"].astype(str)
    + "')\"></i>"
)

points["text"] = (
    e(comment_nl)
    + "<i>"
    + e(points["extra_text"])
    + "</i><br><br>―"
    + points["user_link"]
    + points.ride_datetime.dt.strftime(", %a %d %b %Y, %H:%M").fillna(review_submit_datetime)
)

oldies = points.datetime.dt.year <= 2021
points.loc[oldies, "text"] = (
    e(comment_nl[oldies]) + "―" + points.loc[oldies, "user_link"] + points[oldies].datetime.dt.strftime(", %B %Y").fillna("")
)

groups = points.groupby(["lat", "lon"])

places = groups[["country"]].first()
places["rating"] = groups.rating.mean().round()
places["wait"] = points[~points.wait.isnull()].groupby(["lat", "lon"]).wait.mean()
places["distance"] = points[~points.distance.isnull()].groupby(["lat", "lon"]).distance.mean()
places["text"] = groups.text.apply(lambda t: "<hr>".join(t.dropna()))

places["review_users"] = points.dropna(subset=["text", "hitchhiker"]).groupby(["lat", "lon"]).hitchhiker.unique().apply(list)

places["dest_lats"] = points.dropna(subset=["dest_lat", "dest_lon"]).groupby(["lat", "lon"]).dest_lat.apply(list)
places["dest_lons"] = points.dropna(subset=["dest_lat", "dest_lon"]).groupby(["lat", "lon"]).dest_lon.apply(list)

places.reset_index(inplace=True)
places.sort_values("rating", inplace=True, ascending=False)


def write_json_file(data, filename):
    """Writes a JSON file into the dist folder containing data for the map

    Args:
        data: The data to be converted to JSON
        filename: The filename to be stored into
    """
    filepath = os.path.join(dirs["dist"], filename)
    logger.info(f"Writing: {filepath}")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(simplejson.dumps(data.to_dict(orient="records"), ignore_nan=True))


point_columns = [
    "lat",
    "lon",
    "rating",
    "text",
    "wait",
    "distance",
    "review_users",
    "dest_lats",
    "dest_lons",
]

logger.info("Generating JSON data files")
write_json_file(places[point_columns], "points.json")

places_light = places[(places.text.str.len() > 0) | ~places.distance.isnull()]
write_json_file(places_light[point_columns], "points_light.json")

places_with_destination = places[~places.distance.isnull()]
write_json_file(places_with_destination[point_columns], "points_with_destination.json")

recent = points.dropna(subset=["datetime"]).sort_values("datetime", ascending=False).iloc[:1000]
recent["url"] = "#" + recent.lat.astype(str) + "," + recent.lon.astype(str)
recent["text"] = points.comment.fillna("") + " " + points.extra_text.fillna("")
recent["hitchhiker"] = recent.hitchhiker.str.replace("://", "", regex=False)
recent["distance"] = recent["distance"].round(1)
recent["datetime"] = recent["datetime"].astype(str)
recent["datetime"] += np.where(~recent.ride_datetime.isnull(), " 🕒", "")
write_json_file(recent[["url", "country", "datetime", "hitchhiker", "rating", "distance", "text"]], "points_recent.json")

duplicates["from_url"] = "#" + duplicates.from_lat.astype(str) + "," + duplicates.from_lon.astype(str)
duplicates["to_url"] = "#" + duplicates.to_lat.astype(str) + "," + duplicates.to_lon.astype(str)
duplicates_data = duplicates[["id", "from_url", "to_url", "distance", "reviewed", "accepted"]].to_dict(orient="records")
write_json_file(duplicates[["id", "from_url", "to_url", "distance", "reviewed", "accepted"]], "points_duplicates.json")

logger.info("Script execution completed")
