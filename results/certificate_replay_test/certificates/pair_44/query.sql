SELECT
  a.firstname,
  a.lastname,
  b.city,
  b.state
FROM (
  SELECT
    *
  FROM (
    SELECT
      *
    FROM PERSON
  ) AS A
  LEFT JOIN (
    SELECT
      *
    FROM ADDRESS
  ) AS B
    ON A.PERSONID = B.PERSONID
) AS _subquery