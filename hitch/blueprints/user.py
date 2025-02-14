import pandas as pd
from flask import Blueprint, current_app, jsonify, redirect, render_template
from flask_security import current_user

from hitch.extensions import security
from hitch.forms import UserEditForm
from hitch.helpers import get_db

user_bp = Blueprint("user", __name__)


@user_bp.route("/edit-user", methods=["GET", "POST"])
def form():
    if current_user.is_anonymous:
        return redirect("/login")

    form = UserEditForm()

    if form.validate_on_submit():
        updated_user = security.datastore.find_user(username=current_user.username)
        updated_user.gender = form.gender.data
        updated_user.year_of_birth = form.year_of_birth.data
        updated_user.hitchhiking_since = form.hitchhiking_since.data
        updated_user.origin_country = form.origin_country.data
        updated_user.origin_city = form.origin_city.data
        updated_user.hitchwiki_username = form.hitchwiki_username.data
        updated_user.trustroots_username = form.trustroots_username.data
        security.datastore.put(updated_user)
        security.datastore.commit()
        return redirect("/me")

    form.gender.data = current_user.gender
    form.year_of_birth.data = current_user.year_of_birth
    form.hitchhiking_since.data = current_user.hitchhiking_since
    form.origin_country.data = current_user.origin_country
    form.origin_city.data = current_user.origin_city
    form.hitchwiki_username.data = current_user.hitchwiki_username
    form.trustroots_username.data = current_user.trustroots_username

    return render_template("security/edit_user.html", form=form)


@user_bp.route("/user", methods=["GET"])
def get_user():
    """Endpoint to get the currently logged in user."""
    current_app.logger.info("Received request to get user.")

    # Check if the user is logged in
    if not current_user.is_anonymous:
        return jsonify({"logged_in": True, "username": current_user.username})
    else:
        return jsonify({"logged_in": False, "username": ""})


# TODO: properly delete the user after their confirmation
@user_bp.route("/delete-user", methods=["GET"])
def delete_user():
    return f"To delete your account please send an email to {current_app.config['EMAIL']} with the subject 'Delete my account'."


@user_bp.route("/is_username_used/<username>", methods=["GET"])
def is_username_used(username):
    """Endpoint to check if a username is already used."""
    current_app.logger.info(f"Received request to check if username {username} is used.")

    user = security.datastore.find_user(username=username)

    if user:
        return jsonify({"used": True})
    else:
        return jsonify({"used": False})


@user_bp.route("/me", methods=["GET"], defaults={"username": None, "is_me": True})
@user_bp.route("/account/<username>", methods=["GET"])
def show_account(username, is_me: bool = False):
    """Returns either the current account or the requested user

    Args:
        username: The user to show, None if current_user
        is_me: Whether the current_user should be shown, True if current_user
    """
    if is_me and current_user.is_anonymous:
        return redirect("/login")

    user = current_user if is_me else security.datastore.find_user(username=username)

    current_app.logger.info(
        f"Received request to show user account for {current_user.username}"
        if is_me
        else f"Received request to show user {username}."
    )

    # TODO: Proper 404
    if user is None:
        return "User not found."

    return render_template("security/account.html", user=user, is_me=is_me)


@user_bp.route("/contributors", methods=["GET"])
def contributors():
    query = """select
            u.username AS hitchhiker,
            COUNT(*) AS total_contributions
        from points p left join user u on p.user_id = u.id
        where p.user_id is not null
        group by p.user_id
        order by total_contributions desc"""
    overall_contributions = pd.read_sql(
        query,
        get_db(),
    )
    overall_contributions.index = overall_contributions.index + 1

    query = """select
            u.username AS hitchhiker,
            COUNT(*) AS total_contributions
        from points p left join user u on p.user_id = u.id
        where p.user_id is not null
            and strftime('%Y-%m', p.datetime) = strftime('%Y-%m', 'now')
        group by p.user_id
        order by total_contributions desc;"""
    monthly_contributions = pd.read_sql(
        query,
        get_db(),
    )
    monthly_contributions.index = monthly_contributions.index + 1

    return render_template(
        "security/contributors.html",
        is_logged_in=not current_user.is_anonymous,
        overall_contributions=overall_contributions.to_html(),
        short_overall_contributions=overall_contributions.head(10).to_html(),
        monthly_contributions=monthly_contributions.to_html(),
        short_monthly_contributions=monthly_contributions.head(10).to_html(),
    )


@user_bp.route("/claim-review/<review_id>", methods=["GET", "POST"])
def claim_review(review_id: int):
    """Endpoint to claim a review."""
    current_app.logger.info(f"Received request to claim review {review_id}.")

    if current_user.is_anonymous:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "create table if not exists claims"
        + "(id integer primary key, "
        + "user_id integer, "
        + "review_id integer, "
        + "timestamp timestamp default current_timestamp)"
    )

    # Insert or replace existing entry
    query_result = cursor.execute(f"select user_id from points where id = {review_id}").fetchall()
    if len(query_result) == 0:
        error_message = "Review not found."
    if len(query_result) > 1:
        error_message = "Multiple reviews found."
    elif query_result[0][0] is not None:
        error_message = "Review already claimed."
    else:
        error_message = None

    if error_message:
        conn.close()
        return render_template("security/failed.html", message=error_message)

    claims_today = cursor.execute(
        f"select count(*) from claims where user_id = {current_user.id} and date(timestamp) = date('now')"
    ).fetchone()
    num_claims = claims_today[0] if claims_today else 0
    if num_claims >= current_app.config["MAX_CLAIMS_PER_DAY"]:
        reply = render_template(
            "security/failed.html", message=f"You can only claim {current_app.config['MAX_CLAIMS_PER_DAY']} reviews per day."
        )
    else:
        cursor.execute(f"update points set user_id = {current_user.id} where id = {review_id}")
        cursor.execute(f"insert or replace into claims (user_id, review_id) values ({current_user.id}, {review_id})")
        conn.commit()
        message = f"{num_claims + 1}/{current_app.config['MAX_CLAIMS_PER_DAY']} reviews claimed today."
        reply = render_template("security/success.html", message=message)

    conn.close()

    return reply