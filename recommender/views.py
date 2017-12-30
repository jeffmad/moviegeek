from datetime import datetime
from decimal import Decimal
from math import sqrt

import numpy as np
import operator
import os

from django.http import JsonResponse
from django.db.models import Avg, Count

from analytics.models import Rating
from collector.models import Log
from recommender.models import SeededRecs, Recs, MovieDescriptions, Similarity
from builder import data_helper

from gensim import models, corpora, similarities

from recs.content_based_recommender import ContentBasedRecs
from recs.funksvd_recommender import FunkSVDRecs
from recs.neighborhood_based_recommender import NeighborhoodBasedRecs
from recs.popularity_recommender import PopularityBasedRecs


def get_association_rules_for(request, content_id, take=6):
    data = SeededRecs.objects.filter(source=content_id) \
               .order_by('-confidence') \
               .values('target', 'confidence', 'support')[:take]

    return JsonResponse(dict(data=list(data)), safe=False)


def recs_using_association_rules(request, user_id, take=6):
    events = Log.objects.filter(user_id=user_id)\
                        .order_by('created')\
                        .values_list('content_id', flat=True)\
                        .distinct()

    seeds = set(events[:20])

    rules = SeededRecs.objects.filter(source__in=seeds) \
        .exclude(target__in=seeds) \
        .values('target') \
        .annotate(confidence=Avg('confidence')) \
        .order_by('-confidence')

    recs = [{'id': '{0:07d}'.format(int(rule['target'])),
             'confidence': rule['confidence']} for rule in rules]

    print("recs from association rules: \n{}".format(recs[:take]))
    return JsonResponse(dict(data=list(recs[:take])))


def chart(request, take=10):
    sql = """SELECT content_id,
                mov.title,
                count(*) as sold
            FROM    collector_log log
            JOIN    moviegeeks_movie mov
            ON      log.content_id = mov.movie_id
            WHERE 	event like 'buy'
            GROUP BY content_id, mov.title
            ORDER BY sold desc
            LIMIT {}
            """.format(take)

    c = data_helper.get_query_cursor(sql)
    data = data_helper.dictfetchall(c)

    return JsonResponse(data, safe=False)


def pearson(users, this_user, that_user):
    if this_user in users and that_user in users:
        this_user_avg = sum(users[this_user].values()) / len(users[this_user].values())
        that_user_avg = sum(users[that_user].values()) / len(users[that_user].values())

        all_movies = set(users[this_user].keys()) & set(users[that_user].keys())

        dividend = 0
        a_divisor = 0
        b_divisor = 0
        for movie in all_movies:

            if movie in users[this_user].keys() and movie in users[that_user].keys():
                a_nr = users[this_user][movie] - this_user_avg
                b_nr = users[that_user][movie] - that_user_avg
                dividend += a_nr * b_nr
                a_divisor += pow(a_nr, 2)
                b_divisor += pow(b_nr, 2)

        divisor = Decimal(sqrt(a_divisor) * sqrt(b_divisor))
        if divisor != 0:
            return dividend / Decimal(sqrt(a_divisor) * sqrt(b_divisor))

    return 0


def jaccard(users, this_user, that_user):
    if this_user in users and that_user in users:
        intersect = set(users[this_user].keys()) & set(users[that_user].keys())
        union = set(users[this_user].keys()) | set(users[that_user].keys())

        return len(intersect) / Decimal(len(union))
    else:
        return 0


def similar_users(request, user_id, type):
    min = request.GET.get('min', 1)

    ratings = Rating.objects.filter(user_id=user_id)
    sim_users = Rating.objects.filter(movie_id__in=ratings.values('movie_id')) \
        .values('user_id') \
        .annotate(intersect=Count('user_id')).filter(intersect__gt=min)

    dataset = Rating.objects.filter(user_id__in=sim_users.values('user_id'))

    users = {u['user_id']: {} for u in sim_users}

    for row in dataset:
        if row.user_id in users.keys():
            users[row.user_id][row.movie_id] = row.rating

    similarity = dict()

    switcher = {
        'jaccard': jaccard,
        'pearson': pearson,

    }

    for user in sim_users:

        func = switcher.get(type, lambda: "nothing")
        s = func(users, int(user_id), int(user['user_id']))

        if s > 0.5:
            similarity[user['user_id']] = round(s, 2)
    topn = sorted(similarity.items(), key=operator.itemgetter(1), reverse=True)[:10]

    data = {
        'user_id': user_id,
        'num_movies_rated': len(ratings),
        'type': type,
        'topn': topn,
        'similarity': topn,
    }

    return JsonResponse(data, safe=False)


def similar_content(request, content_id, num=6):

    sorted_items = ContentBasedRecs().seeded_rec([content_id], num)
    data = {
        'source_id': content_id,
        'data': sorted_items
    }

    return JsonResponse(data, safe=False)


def recs_cb(request, user_id, num=6):
    start_time = datetime.now()

    print(f"lda loaded in {datetime.now()-start_time}")
    sorted_items = ContentBasedRecs().recommend_items(user_id, num)

    data = {
        'user_id': user_id,
        'data': sorted_items
    }

    return JsonResponse(data, safe=False)


def recs_funksvd(request, user_id, num=6):
    sorted_items = FunkSVDRecs().recommend_items(user_id, num)

    data = {
        'user_id': user_id,
        'data': sorted_items
    }
    return JsonResponse(data, safe=False)


def recs_cf(request, user_id, num=6):
    min_sim = request.GET.get('min_sim', 0.1)
    sorted_items = NeighborhoodBasedRecs(min_sim=min_sim).recommend_items(user_id, num)

    print(f"cf sorted_items is: {sorted_items}")
    data = {
        'user_id': user_id,
        'data': sorted_items
    }

    return JsonResponse(data, safe=False)


def recs_pop(request, user_id, num=60):
    top_num = PopularityBasedRecs().recommend_items(user_id, num)
    data = {
        'user_id': user_id,
        'data': top_num[:num]
    }

    return JsonResponse(data, safe=False)


def get_movie_ids(sorted_sims, corpus, dictionary):
    ids = [s[0] for s in sorted_sims]
    movies = MovieDescriptions.objects.filter(lda_vector__in=ids)

    return [{"target": movies[i].imdb_id,
             "title": movies[i].title,
             "sim": str(sorted_sims[i][1])} for i in range(len(movies))]


def lda2array(lda_vector, len):
    vec = np.zeros(len)
    for coor in lda_vector:
        if coor[0] > 1270:
            print("auc")
        vec[coor[0]] = coor[1]

    return vec
