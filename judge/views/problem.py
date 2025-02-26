import logging
import os
import shutil
from datetime import timedelta, datetime
from operator import itemgetter
from random import randrange
import random

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q, Sum, Case, When, IntegerField
from django.db.utils import ProgrammingError
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.functional import cached_property
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import ListView, View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from judge.comments import CommentedDetailView
from judge.forms import ProblemCloneForm, ProblemSubmitForm, ProblemPointsVoteForm
from judge.models import (
    ContestProblem,
    ContestSubmission,
    Judge,
    Language,
    Problem,
    ContestProblemClarification,
    ProblemGroup,
    ProblemTranslation,
    ProblemType,
    ProblemPointsVote,
    RuntimeVersion,
    Solution,
    Submission,
    SubmissionSource,
    TranslatedProblemForeignKeyQuerySet,
    Organization,
    VolunteerProblemVote,
    Profile,
    LanguageTemplate,
)
from judge.pdf_problems import DefaultPdfMaker, HAS_PDF
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import (
    contest_attempted_ids,
    contest_completed_ids,
    hot_problems,
    user_attempted_ids,
    user_completed_ids,
)
from judge.utils.strings import safe_float_or_none, safe_int_or_none
from judge.utils.tickets import own_ticket_filter
from judge.utils.views import (
    QueryStringSortMixin,
    SingleObjectFormView,
    TitleMixin,
    generic_message,
)
from judge.ml.collab_filter import CollabFilter


def get_contest_problem(problem, profile):
    try:
        return problem.contests.get(contest_id=profile.current_contest.contest_id)
    except ObjectDoesNotExist:
        return None


def get_contest_submission_count(problem, profile, virtual):
    return (
        profile.current_contest.submissions.exclude(submission__status__in=["IE"])
        .filter(problem__problem=problem, participation__virtual=virtual)
        .count()
    )


class ProblemMixin(object):
    model = Problem
    slug_url_kwarg = "problem"
    slug_field = "code"

    def get_object(self, queryset=None):
        problem = super(ProblemMixin, self).get_object(queryset)
        if not problem.is_accessible_by(self.request.user):
            raise Http404()
        return problem

    def no_such_problem(self):
        code = self.kwargs.get(self.slug_url_kwarg, None)
        return generic_message(
            self.request,
            _("No such problem"),
            _('Could not find a problem with the code "%s".') % code,
            status=404,
        )

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemMixin, self).get(request, *args, **kwargs)
        except Http404 as e:
            print(e)
            return self.no_such_problem()


class SolvedProblemMixin(object):
    def get_completed_problems(self):
        if self.in_contest:
            return contest_completed_ids(self.profile.current_contest)
        else:
            return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        if self.in_contest:
            return contest_attempted_ids(self.profile.current_contest)
        else:
            return user_attempted_ids(self.profile) if self.profile is not None else ()

    def get_latest_attempted_problems(self, limit=None):
        if self.in_contest or not self.profile:
            return ()
        result = list(user_attempted_ids(self.profile).values())
        result = sorted(result, key=lambda d: -d["last_submission"])
        if limit:
            result = result[:limit]
        return result

    @cached_property
    def in_contest(self):
        return (
            self.profile is not None
            and self.profile.current_contest is not None
            and self.request.in_contest_mode
        )

    @cached_property
    def contest(self):
        return self.request.profile.current_contest.contest

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile


class ProblemSolution(
    SolvedProblemMixin, ProblemMixin, TitleMixin, CommentedDetailView
):
    context_object_name = "problem"
    template_name = "problem/editorial.html"

    def get_title(self):
        return _("Editorial for {0}").format(self.object.name)

    def get_content_title(self):
        return format_html(
            _('Editorial for <a href="{1}">{0}</a>'),
            self.object.name,
            reverse("problem_detail", args=[self.object.code]),
        )

    def get_context_data(self, **kwargs):
        context = super(ProblemSolution, self).get_context_data(**kwargs)

        solution = get_object_or_404(Solution, problem=self.object)

        if (
            not solution.is_public or solution.publish_on > timezone.now()
        ) and not self.request.user.has_perm("judge.see_private_solution"):
            raise Http404()

        context["solution"] = solution
        context["has_solved_problem"] = self.object.id in self.get_completed_problems()
        return context

    def get_comment_page(self):
        return "s:" + self.object.code


class ProblemRaw(
    ProblemMixin, TitleMixin, TemplateResponseMixin, SingleObjectMixin, View
):
    context_object_name = "problem"
    template_name = "problem/raw.html"

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ProblemRaw, self).get_context_data(**kwargs)
        context["problem_name"] = self.object.name
        context["url"] = self.request.build_absolute_uri()
        context["description"] = self.object.description
        if hasattr(self.object, "data_files"):
            context["fileio_input"] = self.object.data_files.fileio_input
            context["fileio_output"] = self.object.data_files.fileio_output
        else:
            context["fileio_input"] = None
            context["fileio_output"] = None
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        with translation.override(settings.LANGUAGE_CODE):
            return self.render_to_response(
                self.get_context_data(
                    object=self.object,
                )
            )


class ProblemDetail(ProblemMixin, SolvedProblemMixin, CommentedDetailView):
    context_object_name = "problem"
    template_name = "problem/problem.html"

    def get_comment_page(self):
        return "p:%s" % self.object.code

    def get_context_data(self, **kwargs):
        context = super(ProblemDetail, self).get_context_data(**kwargs)
        user = self.request.user
        authed = user.is_authenticated
        context["has_submissions"] = (
            authed
            and Submission.objects.filter(
                user=user.profile, problem=self.object
            ).exists()
        )
        contest_problem = (
            None
            if not authed or user.profile.current_contest is None
            else get_contest_problem(self.object, user.profile)
        )
        context["contest_problem"] = contest_problem

        if contest_problem:
            clarifications = contest_problem.clarifications
            context["has_clarifications"] = clarifications.count() > 0
            context["clarifications"] = clarifications.order_by("-date")
            context["submission_limit"] = contest_problem.max_submissions
            if contest_problem.max_submissions:
                context["submissions_left"] = max(
                    contest_problem.max_submissions
                    - get_contest_submission_count(
                        self.object, user.profile, user.profile.current_contest.virtual
                    ),
                    0,
                )

        context["available_judges"] = Judge.objects.filter(
            online=True, problems=self.object
        )
        context["show_languages"] = (
            self.object.allowed_languages.count() != Language.objects.count()
        )
        context["has_pdf_render"] = HAS_PDF
        context["completed_problem_ids"] = self.get_completed_problems()
        context["attempted_problems"] = self.get_attempted_problems()

        can_edit = self.object.is_editable_by(user)
        context["can_edit_problem"] = can_edit
        if user.is_authenticated:
            tickets = self.object.tickets
            if not can_edit:
                tickets = tickets.filter(own_ticket_filter(user.profile.id))
            context["has_tickets"] = tickets.exists()
            context["num_open_tickets"] = (
                tickets.filter(is_open=True).values("id").distinct().count()
            )

        try:
            context["editorial"] = Solution.objects.get(problem=self.object)
        except ObjectDoesNotExist:
            pass
        try:
            translation = self.object.translations.get(
                language=self.request.LANGUAGE_CODE
            )
        except ProblemTranslation.DoesNotExist:
            context["title"] = self.object.name
            context["language"] = settings.LANGUAGE_CODE
            context["description"] = self.object.description
            context["translated"] = False
        else:
            context["title"] = translation.name
            context["language"] = self.request.LANGUAGE_CODE
            context["description"] = translation.description
            context["translated"] = True

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph(
                "generated-meta-problem:%s:%d" % (context["language"], self.object.id),
                context["description"],
            )
        context["meta_description"] = self.object.summary or metadata[0]
        context["og_image"] = self.object.og_image or metadata[1]
        if hasattr(self.object, "data_files"):
            context["fileio_input"] = self.object.data_files.fileio_input
            context["fileio_output"] = self.object.data_files.fileio_output
        else:
            context["fileio_input"] = None
            context["fileio_output"] = None

        return context


class LatexError(Exception):
    pass


class ProblemPdfView(ProblemMixin, SingleObjectMixin, View):
    logger = logging.getLogger("judge.problem.pdf")
    languages = set(map(itemgetter(0), settings.LANGUAGES))

    def get(self, request, *args, **kwargs):
        if not HAS_PDF:
            raise Http404()

        language = kwargs.get("language", self.request.LANGUAGE_CODE)
        if language not in self.languages:
            raise Http404()

        problem = self.get_object()
        try:
            trans = problem.translations.get(language=language)
        except ProblemTranslation.DoesNotExist:
            trans = None

        cache = os.path.join(
            settings.DMOJ_PDF_PROBLEM_CACHE, "%s.%s.pdf" % (problem.code, language)
        )

        if not os.path.exists(cache):
            self.logger.info("Rendering: %s.%s.pdf", problem.code, language)
            with DefaultPdfMaker() as maker, translation.override(language):
                problem_name = problem.name if trans is None else trans.name
                maker.html = (
                    get_template("problem/raw.html")
                    .render(
                        {
                            "problem": problem,
                            "problem_name": problem_name,
                            "description": problem.description
                            if trans is None
                            else trans.description,
                            "url": request.build_absolute_uri(),
                            "math_engine": maker.math_engine,
                        }
                    )
                    .replace('"//', '"https://')
                    .replace("'//", "'https://")
                )
                maker.title = problem_name
                assets = ["style.css", "pygment-github.css"]
                if maker.math_engine == "jax":
                    assets.append("mathjax_config.js")
                for file in assets:
                    maker.load(file, os.path.join(settings.DMOJ_RESOURCES, file))
                maker.make()
                if not maker.success:
                    self.logger.error("Failed to render PDF for %s", problem.code)
                    return HttpResponse(
                        maker.log, status=500, content_type="text/plain"
                    )
                shutil.move(maker.pdffile, cache)
        response = HttpResponse()
        if hasattr(settings, "DMOJ_PDF_PROBLEM_INTERNAL") and request.META.get(
            "SERVER_SOFTWARE", ""
        ).startswith("nginx/"):
            response["X-Accel-Redirect"] = "%s/%s.%s.pdf" % (
                settings.DMOJ_PDF_PROBLEM_INTERNAL,
                problem.code,
                language,
            )
        else:
            with open(cache, "rb") as f:
                response.content = f.read()

        response["Content-Type"] = "application/pdf"
        response["Content-Disposition"] = "inline; filename=%s.%s.pdf" % (
            problem.code,
            language,
        )
        return response


class ProblemPdfDescriptionView(ProblemMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        problem = self.get_object()
        if not problem.pdf_description:
            raise Http404()
        response = HttpResponse()
        if request.META.get("SERVER_SOFTWARE", "").startswith("nginx/"):
            response["X-Accel-Redirect"] = problem.pdf_description.path
        else:
            with open(problem.pdf_description.path, "rb") as f:
                response.content = f.read()

        response["Content-Type"] = "application/pdf"
        response["Content-Disposition"] = "inline; filename=%s.pdf" % (problem.code,)
        return response


class ProblemList(QueryStringSortMixin, TitleMixin, SolvedProblemMixin, ListView):
    model = Problem
    title = gettext_lazy("Problems")
    context_object_name = "problems"
    template_name = "problem/list.html"
    paginate_by = 50
    sql_sort = frozenset(("date", "points", "ac_rate", "user_count", "code"))
    manual_sort = frozenset(("name", "group", "solved", "type"))
    all_sorts = sql_sort | manual_sort
    default_desc = frozenset(("date", "points", "ac_rate", "user_count"))
    default_sort = "-date"
    first_page_href = None

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        paginator = DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs
        )
        if not self.in_contest:
            # Get the number of pages and then add in this magic.
            # noinspection PyStatementEffect
            paginator.num_pages

            queryset = queryset.add_i18n_name(self.request.LANGUAGE_CODE)
            sort_key = self.order.lstrip("-")
            if sort_key in self.sql_sort:
                queryset = queryset.order_by(self.order)
            elif sort_key == "name":
                queryset = queryset.order_by(self.order.replace("name", "i18n_name"))
            elif sort_key == "group":
                queryset = queryset.order_by(self.order + "__name")
            elif sort_key == "solved":
                if self.request.user.is_authenticated:
                    profile = self.request.profile
                    solved = user_completed_ids(profile)
                    attempted = user_attempted_ids(profile)

                    def _solved_sort_order(problem):
                        if problem.id in solved:
                            return 1
                        if problem.id in attempted:
                            return 0
                        return -1

                    queryset = list(queryset)
                    queryset.sort(
                        key=_solved_sort_order, reverse=self.order.startswith("-")
                    )
            elif sort_key == "type":
                if self.show_types:
                    queryset = list(queryset)
                    queryset.sort(
                        key=lambda problem: problem.types_list[0]
                        if problem.types_list
                        else "",
                        reverse=self.order.startswith("-"),
                    )
            paginator.object_list = queryset
        return paginator

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    def get_contest_queryset(self):
        queryset = (
            self.profile.current_contest.contest.contest_problems.select_related(
                "problem__group"
            )
            .defer("problem__description")
            .order_by("problem__code")
            .annotate(user_count=Count("submission__participation", distinct=True))
            .order_by("order")
        )
        queryset = TranslatedProblemForeignKeyQuerySet.add_problem_i18n_name(
            queryset, "i18n_name", self.request.LANGUAGE_CODE, "problem__name"
        )
        return [
            {
                "id": p["problem_id"],
                "code": p["problem__code"],
                "name": p["problem__name"],
                "i18n_name": p["i18n_name"],
                "group": {"full_name": p["problem__group__full_name"]},
                "points": p["points"],
                "partial": p["partial"],
                "user_count": p["user_count"],
            }
            for p in queryset.values(
                "problem_id",
                "problem__code",
                "problem__name",
                "i18n_name",
                "problem__group__full_name",
                "points",
                "partial",
                "user_count",
            )
        ]

    def get_org_query(self, query):
        if not self.profile:
            return []
        return [
            i
            for i in query
            if i in self.profile.organizations.values_list("id", flat=True)
        ]

    def get_normal_queryset(self):
        filter = Q(is_public=True)
        if self.profile is not None:
            filter |= Q(authors=self.profile)
            filter |= Q(curators=self.profile)
            filter |= Q(testers=self.profile)
        queryset = (
            Problem.objects.filter(filter).select_related("group").defer("description")
        )
        if not self.request.user.has_perm("see_organization_problem"):
            filter = Q(is_organization_private=False)
            if self.profile is not None:
                filter |= Q(organizations__in=self.profile.organizations.all())
            queryset = queryset.filter(filter)
        if self.profile is not None and self.hide_solved:
            queryset = queryset.exclude(
                id__in=Submission.objects.filter(
                    user=self.profile, points=F("problem__points")
                ).values_list("problem__id", flat=True)
            )
        if self.org_query:
            self.org_query = self.get_org_query(self.org_query)
            print(self.org_query)
            queryset = queryset.filter(
                Q(organizations__in=self.org_query)
                | Q(contests__contest__organizations__in=self.org_query)
            )
        if self.author_query:
            queryset = queryset.filter(authors__in=self.author_query)
        if self.show_types:
            queryset = queryset.prefetch_related("types")
        if self.category is not None:
            queryset = queryset.filter(group__id=self.category)
        if self.selected_types:
            queryset = queryset.filter(types__in=self.selected_types)
        if "search" in self.request.GET:
            self.search_query = query = " ".join(
                self.request.GET.getlist("search")
            ).strip()
            if query:
                if settings.ENABLE_FTS and self.full_text:
                    queryset = queryset.search(query, queryset.BOOLEAN).extra(
                        order_by=["-relevance"]
                    )
                else:
                    queryset = queryset.filter(
                        Q(code__icontains=query)
                        | Q(name__icontains=query)
                        | Q(
                            translations__name__icontains=query,
                            translations__language=self.request.LANGUAGE_CODE,
                        )
                    )
        self.prepoint_queryset = queryset
        if self.point_start is not None:
            queryset = queryset.filter(points__gte=self.point_start)
        if self.point_end is not None:
            queryset = queryset.filter(points__lte=self.point_end)
        queryset = queryset.annotate(
            has_public_editorial=Sum(
                Case(
                    When(solution__is_public=True, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            )
        )

        return queryset.distinct()

    def get_queryset(self):
        if self.in_contest:
            return self.get_contest_queryset()
        else:
            return self.get_normal_queryset()

    def get_context_data(self, **kwargs):
        context = super(ProblemList, self).get_context_data(**kwargs)

        context["hide_solved"] = 0 if self.in_contest else int(self.hide_solved)
        context["show_types"] = 0 if self.in_contest else int(self.show_types)
        context["full_text"] = 0 if self.in_contest else int(self.full_text)
        context["show_editorial"] = 0 if self.in_contest else int(self.show_editorial)
        context["have_editorial"] = 0 if self.in_contest else int(self.have_editorial)
        context["show_solved_only"] = (
            0 if self.in_contest else int(self.show_solved_only)
        )

        if self.request.profile:
            context["organizations"] = self.request.profile.organizations.all()
        all_authors_ids = set(Problem.objects.values_list("authors", flat=True))
        context["all_authors"] = Profile.objects.filter(id__in=all_authors_ids)
        context["category"] = self.category
        context["categories"] = ProblemGroup.objects.all()
        if self.show_types:
            context["selected_types"] = self.selected_types
            context["problem_types"] = ProblemType.objects.all()
        context["has_fts"] = settings.ENABLE_FTS
        context["org_query"] = self.org_query
        context["author_query"] = self.author_query
        context["search_query"] = self.search_query
        context["completed_problem_ids"] = self.get_completed_problems()
        context["attempted_problems"] = self.get_attempted_problems()
        context["last_attempted_problems"] = self.get_latest_attempted_problems(15)
        context["page_type"] = "list"
        context.update(self.get_sort_paginate_context())
        if not self.in_contest:
            context.update(self.get_sort_context())
            (
                context["point_start"],
                context["point_end"],
                context["point_values"],
            ) = self.get_noui_slider_points()
        else:
            context["point_start"], context["point_end"], context["point_values"] = (
                0,
                0,
                {},
            )
            context["hide_contest_scoreboard"] = self.contest.scoreboard_visibility in (
                self.contest.SCOREBOARD_AFTER_CONTEST,
                self.contest.SCOREBOARD_AFTER_PARTICIPATION,
            )
            context["has_clarifications"] = False

            if self.request.user.is_authenticated:
                participation = self.request.profile.current_contest
                if participation:
                    clarifications = ContestProblemClarification.objects.filter(
                        problem__in=participation.contest.contest_problems.all()
                    )
                    context["has_clarifications"] = clarifications.count() > 0
                    context["clarifications"] = clarifications.order_by("-date")
                    if participation.contest.is_editable_by(self.request.user):
                        context["can_edit_contest"] = True

        context["page_prefix"] = None
        context["page_suffix"] = suffix = (
            ("?" + self.request.GET.urlencode()) if self.request.GET else ""
        )
        context["first_page_href"] = (self.first_page_href or ".") + suffix
        context["has_show_editorial_option"] = True
        return context

    def get_noui_slider_points(self):
        points = sorted(
            self.prepoint_queryset.values_list("points", flat=True).distinct()
        )
        if not points:
            return 0, 0, {}
        if len(points) == 1:
            return (
                points[0],
                points[0],
                {
                    "min": points[0] - 1,
                    "max": points[0] + 1,
                },
            )

        start, end = points[0], points[-1]
        if self.point_start is not None:
            start = self.point_start
        if self.point_end is not None:
            end = self.point_end
        points_map = {0.0: "min", 1.0: "max"}
        size = len(points) - 1
        return (
            start,
            end,
            {
                points_map.get(i / size, "%.2f%%" % (100 * i / size,)): j
                for i, j in enumerate(points)
            },
        )

    def GET_with_session(self, request, key):
        if not request.GET:
            return request.session.get(key, False)
        return request.GET.get(key, None) == "1"

    def setup_problem_list(self, request):
        self.hide_solved = self.GET_with_session(request, "hide_solved")
        self.show_types = self.GET_with_session(request, "show_types")
        self.full_text = self.GET_with_session(request, "full_text")
        self.show_editorial = self.GET_with_session(request, "show_editorial")
        self.have_editorial = self.GET_with_session(request, "have_editorial")
        self.show_solved_only = self.GET_with_session(request, "show_solved_only")

        self.search_query = None
        self.category = None
        self.org_query = []
        self.author_query = []
        self.selected_types = []

        # This actually copies into the instance dictionary...
        self.all_sorts = set(self.all_sorts)
        if not self.show_types:
            self.all_sorts.discard("type")

        self.category = safe_int_or_none(request.GET.get("category"))
        if "type" in request.GET:
            try:
                self.selected_types = list(map(int, request.GET.getlist("type")))
            except ValueError:
                pass
        if "orgs" in request.GET:
            try:
                self.org_query = list(map(int, request.GET.getlist("orgs")))
            except ValueError:
                pass
        if "authors" in request.GET:
            try:
                self.author_query = list(map(int, request.GET.getlist("authors")))
            except ValueError:
                pass

        self.point_start = safe_float_or_none(request.GET.get("point_start"))
        self.point_end = safe_float_or_none(request.GET.get("point_end"))

    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)

        try:
            return super(ProblemList, self).get(request, *args, **kwargs)
        except ProgrammingError as e:
            return generic_message(request, "FTS syntax error", e.args[1], status=400)

    def post(self, request, *args, **kwargs):
        to_update = (
            "hide_solved",
            "show_types",
            "full_text",
            "show_editorial",
            "have_editorial",
            "show_solved_only",
        )
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == "1"
                request.session[key] = val
            else:
                request.session[key] = False
        return HttpResponseRedirect(request.get_full_path())


cf_logger = logging.getLogger("judge.ml.collab_filter")


class ProblemFeed(ProblemList):
    model = Problem
    context_object_name = "problems"
    template_name = "problem/feed.html"
    paginate_by = 20
    title = _("Problem feed")
    feed_type = None

    def GET_with_session(self, request, key):
        if not request.GET:
            return request.session.get(key, key == "hide_solved")
        return request.GET.get(key, None) == "1"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        return DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs
        )

    # arr = [[], [], ..]
    def merge_recommendation(self, arr):
        seed = datetime.now().strftime("%d%m%Y")
        merged_array = []
        for a in arr:
            merged_array += a
        random.Random(seed).shuffle(merged_array)

        res = []
        used_pid = set()

        for obj in merged_array:
            if type(obj) == tuple:
                obj = obj[1]
            if obj not in used_pid:
                res.append(obj)
                used_pid.add(obj)
        return res

    def get_queryset(self):
        if self.feed_type == "volunteer":
            self.hide_solved = 0
            self.show_types = 1
        queryset = super(ProblemFeed, self).get_queryset()

        if self.have_editorial:
            queryset = queryset.filter(has_public_editorial=1)

        user = self.request.profile

        if self.feed_type == "new":
            return queryset.order_by("-date")
        elif user and self.feed_type == "volunteer":
            voted_problems = (
                user.volunteer_problem_votes.values_list("problem", flat=True)
                if not bool(self.search_query)
                else []
            )
            if self.show_solved_only:
                queryset = queryset.filter(
                    id__in=Submission.objects.filter(
                        user=self.profile, points=F("problem__points")
                    ).values_list("problem__id", flat=True)
                )
            return queryset.exclude(id__in=voted_problems).order_by("?")
        if not settings.ML_OUTPUT_PATH or not user:
            return queryset.order_by("?")

        # Logging
        log_data = {
            "user": self.request.user.username,
            "cf": {
                "dot": {},
                "cosine": {},
            },
            "cf_time": {"dot": {}, "cosine": {}},
        }

        cf_model = CollabFilter("collab_filter", log_time=log_data["cf"])
        cf_time_model = CollabFilter("collab_filter_time", log_time=log_data["cf_time"])
        hot_problems_recommendations = [
            problem
            for problem in hot_problems(timedelta(days=7), 20)
            if problem in queryset
        ]

        q = self.merge_recommendation(
            [
                cf_model.user_recommendations(
                    user, queryset, cf_model.DOT, 100, log_time=log_data["cf"]["dot"]
                ),
                cf_model.user_recommendations(
                    user,
                    queryset,
                    cf_model.COSINE,
                    100,
                    log_time=log_data["cf"]["cosine"],
                ),
                cf_time_model.user_recommendations(
                    user,
                    queryset,
                    cf_time_model.COSINE,
                    100,
                    log_time=log_data["cf_time"]["cosine"],
                ),
                cf_time_model.user_recommendations(
                    user,
                    queryset,
                    cf_time_model.DOT,
                    100,
                    log_time=log_data["cf_time"]["dot"],
                ),
                hot_problems_recommendations,
            ]
        )

        cf_logger.info(log_data)
        return q

    def get_context_data(self, **kwargs):
        context = super(ProblemFeed, self).get_context_data(**kwargs)
        context["page_type"] = "feed"
        context["title"] = self.title
        context["feed_type"] = self.feed_type
        context["has_show_editorial_option"] = False
        context["has_have_editorial_option"] = False

        return context

    def get(self, request, *args, **kwargs):
        if request.in_contest_mode:
            return HttpResponseRedirect(reverse("problem_list"))
        return super(ProblemFeed, self).get(request, *args, **kwargs)


class LanguageTemplateAjax(View):
    def get(self, request, *args, **kwargs):
        try:
            problem = request.GET.get("problem", None)
            lang_id = int(request.GET.get("id", 0))
            res = None
            if problem:
                try:
                    res = LanguageTemplate.objects.get(
                        language__id=lang_id, problem__id=problem
                    ).source
                except ObjectDoesNotExist:
                    pass
            if not res:
                res = get_object_or_404(Language, id=lang_id).template
        except ValueError:
            raise Http404()
        return HttpResponse(res, content_type="text/plain")


class RandomProblem(ProblemList):
    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)
        if self.in_contest:
            raise Http404()

        queryset = self.get_normal_queryset()
        count = queryset.count()
        if not count:
            return HttpResponseRedirect(
                "%s%s%s"
                % (
                    reverse("problem_list"),
                    request.META["QUERY_STRING"] and "?",
                    request.META["QUERY_STRING"],
                )
            )
        return HttpResponseRedirect(queryset[randrange(count)].get_absolute_url())


user_logger = logging.getLogger("judge.user")


@login_required
def problem_submit(request, problem, submission=None):
    if (
        submission is not None
        and not request.user.has_perm("judge.resubmit_other")
        and get_object_or_404(Submission, id=int(submission)).user.user != request.user
    ):
        raise PermissionDenied()

    profile = request.profile
    problem = get_object_or_404(Problem, code=problem)
    if not problem.is_accessible_by(request.user):
        if request.method == "POST":
            user_logger.info(
                "Naughty user %s wants to submit to %s without permission",
                request.user.username,
                problem.code,
            )
            return HttpResponseForbidden("<h1>Not allowed to submit. Try later.</h1>")
        raise Http404()

    if problem.is_editable_by(request.user):
        judge_choices = tuple(
            Judge.objects.filter(online=True, problems=problem).values_list(
                "name", "name"
            )
        )
    else:
        judge_choices = ()

    if request.method == "POST":
        form = ProblemSubmitForm(
            request.POST,
            judge_choices=judge_choices,
            instance=Submission(user=profile, problem=problem),
        )
        if form.is_valid():
            if (
                not request.user.has_perm("judge.spam_submission")
                and Submission.objects.filter(user=profile, was_rejudged=False)
                .exclude(status__in=["D", "IE", "CE", "AB"])
                .count()
                >= settings.DMOJ_SUBMISSION_LIMIT
            ):
                return HttpResponse(
                    "<h1>You submitted too many submissions.</h1>", status=429
                )
            if not problem.allowed_languages.filter(
                id=form.cleaned_data["language"].id
            ).exists():
                raise PermissionDenied()
            if (
                not request.user.is_superuser
                and problem.banned_users.filter(id=profile.id).exists()
            ):
                return generic_message(
                    request,
                    _("Banned from submitting"),
                    _(
                        "You have been declared persona non grata for this problem. "
                        "You are permanently barred from submitting this problem."
                    ),
                )

            with transaction.atomic():
                if profile.current_contest is not None:
                    contest_id = profile.current_contest.contest_id
                    try:
                        contest_problem = problem.contests.get(contest_id=contest_id)
                    except ContestProblem.DoesNotExist:
                        model = form.save()
                    else:
                        max_subs = contest_problem.max_submissions
                        if (
                            max_subs
                            and get_contest_submission_count(
                                problem, profile, profile.current_contest.virtual
                            )
                            >= max_subs
                        ):
                            return generic_message(
                                request,
                                _("Too many submissions"),
                                _(
                                    "You have exceeded the submission limit for this problem."
                                ),
                            )
                        model = form.save()
                        model.contest_object_id = contest_id

                        contest = ContestSubmission(
                            submission=model,
                            problem=contest_problem,
                            participation=profile.current_contest,
                        )
                        contest.save()
                else:
                    model = form.save()

                # Create the SubmissionSource object
                source = SubmissionSource(
                    submission=model, source=form.cleaned_data["source"]
                )
                source.save()
                profile.update_contest()

            # Save a query
            model.source = source
            model.judge(rejudge=False, judge_id=form.cleaned_data["judge"])

            return HttpResponseRedirect(
                reverse("submission_status", args=[str(model.id)])
            )
        else:
            form_data = form.cleaned_data
            if submission is not None:
                sub = get_object_or_404(Submission, id=int(submission))
    else:
        initial = {"language": profile.language}
        if submission is not None:
            try:
                sub = get_object_or_404(
                    Submission.objects.select_related("source", "language"),
                    id=int(submission),
                )
                initial["source"] = sub.source.source
                initial["language"] = sub.language
            except ValueError:
                raise Http404()
        form = ProblemSubmitForm(judge_choices=judge_choices, initial=initial)
        form_data = initial
    form.fields["language"].queryset = problem.usable_languages.order_by(
        "name", "key"
    ).prefetch_related(
        Prefetch("runtimeversion_set", RuntimeVersion.objects.order_by("priority"))
    )
    if "language" in form_data:
        form.fields["source"].widget.mode = form_data["language"].ace
    form.fields["source"].widget.theme = profile.ace_theme

    if submission is not None:
        default_lang = sub.language
    else:
        default_lang = request.profile.language

    submission_limit = submissions_left = None
    if profile.current_contest is not None:
        try:
            submission_limit = problem.contests.get(
                contest=profile.current_contest.contest
            ).max_submissions
        except ContestProblem.DoesNotExist:
            pass
        else:
            if submission_limit:
                submissions_left = submission_limit - get_contest_submission_count(
                    problem, profile, profile.current_contest.virtual
                )
    return render(
        request,
        "problem/submit.html",
        {
            "form": form,
            "title": _("Submit to %(problem)s")
            % {
                "problem": problem.translated_name(request.LANGUAGE_CODE),
            },
            "content_title": mark_safe(
                escape(_("Submit to %(problem)s"))
                % {
                    "problem": format_html(
                        '<a href="{0}">{1}</a>',
                        reverse("problem_detail", args=[problem.code]),
                        problem.translated_name(request.LANGUAGE_CODE),
                    ),
                }
            ),
            "langs": Language.objects.all(),
            "no_judges": not form.fields["language"].queryset,
            "submission_limit": submission_limit,
            "submissions_left": submissions_left,
            "ACE_URL": settings.ACE_URL,
            "default_lang": default_lang,
            "problem_id": problem.id,
        },
    )


class ProblemClone(
    ProblemMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView
):
    title = _("Clone Problem")
    template_name = "problem/clone.html"
    form_class = ProblemCloneForm
    permission_required = "judge.clone_problem"

    def form_valid(self, form):
        problem = self.object

        languages = problem.allowed_languages.all()
        language_limits = problem.language_limits.all()
        types = problem.types.all()
        problem.pk = None
        problem.is_public = False
        problem.ac_rate = 0
        problem.user_count = 0
        problem.code = form.cleaned_data["code"]
        problem.save()
        problem.authors.add(self.request.profile)
        problem.allowed_languages.set(languages)
        problem.language_limits.set(language_limits)
        problem.types.set(types)

        return HttpResponseRedirect(
            reverse("admin:judge_problem_change", args=(problem.id,))
        )
