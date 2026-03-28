SELECT
  p.firstname AS FIRSTNAME,
  p.lastname AS LASTNAME,
  a.city AS CITY,
  a.state AS STATE
FROM (
  SELECT
    *
  FROM PERSON
) AS P
LEFT JOIN (
  SELECT
    *
  FROM ADDRESS
) AS A
  ON a.personid = p.personid