from flask import Blueprint, request

bp = Blueprint("example", __name__)


@bp.route("/v<int:api_ver>/widgets", methods=["GET", "POST"])
def widgets(api_ver):
    if request.method == "POST":
        return {"created": True}, 201
    return {"items": []}
