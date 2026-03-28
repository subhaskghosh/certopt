SELECT
  a.firstname,
  a.lastname,
  b.city,
  b.state
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
  ON a.personid = b.personid