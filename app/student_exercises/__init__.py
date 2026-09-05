"""Tuteur IA de résolution d'exercice, côté élève (dernière brique du J5).

Un élève **authentifié** (le régime anonyme n'a pas d'IA — décision actée)
soumet, question par question, sa réponse ou un message d'aide ; le tuteur
évalue la réponse contre le corrigé du professeur, juge l'effort fourni,
guide en s'appuyant sur le cours **sans jamais donner la réponse**, et ne
révèle le corrigé que lorsque la réponse est juste ou l'effort suffisant —
le dévoilement étant décidé par le BACK sur le verdict structuré du modèle.

L'accès au cours est celui du régime public (visibilité + token de partage,
:func:`app.public.access.get_public_course`), mais les routes portent le JWT
de l'élève : l'appel IA est imputé à SA config (credential BYO ou IA par
défaut sous son quota — cascade ``effective_config`` inchangée) et les
tentatives sont persistées à son nom (:mod:`app.models.exercise_submission`).
"""
